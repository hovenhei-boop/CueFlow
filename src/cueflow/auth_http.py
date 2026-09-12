from __future__ import annotations

import hmac
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from functools import wraps
from typing import Any, TypeVar, cast
from urllib.parse import urlsplit

from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from cueflow.auth import (
    AuthenticatedSession,
    AuthService,
    PhoneContinueKind,
    PhoneContinueResult,
    VerificationGrant,
)
from cueflow.errors import (
    AccountError,
    AccountMigrationError,
    AuthenticationError,
    AuthenticationRateLimitedError,
    ContractError,
    CueFlowError,
    IdentityConflictError,
    PhoneBlockedError,
    PhoneReputationIntegrityError,
    SmsProviderUnavailableError,
)
from cueflow.sms import SmsPurpose

ACCESS_COOKIE = "__Host-cueflow-access"
REFRESH_COOKIE = "__Host-cueflow-refresh"
CSRF_COOKIE = "__Host-cueflow-csrf"
CSRF_HEADER = "X-CSRF-Token"
CLIENT_HEADER = "X-CueFlow-Client"

T = TypeVar("T")
ServiceFactory = Callable[[], AuthService]
Endpoint = Callable[[Request], Awaitable[Response]]


@dataclass(frozen=True)
class AuthHttpConfig:
    allowed_origins: frozenset[str]
    max_json_bytes: int = 16 * 1024

    def __post_init__(self) -> None:
        if not self.allowed_origins:
            raise ContractError("Auth HTTP requires at least one allowed HTTPS Origin")
        if self.max_json_bytes < 1:
            raise ContractError("Auth HTTP max_json_bytes must be positive")
        for origin in self.allowed_origins:
            parsed = urlsplit(origin)
            if (
                parsed.scheme != "https"
                or not parsed.netloc
                or parsed.username is not None
                or parsed.password is not None
                or parsed.path
                or parsed.query
                or parsed.fragment
            ):
                raise ContractError("Auth HTTP allowed Origins must be exact HTTPS origins")


class _HttpRequestError(CueFlowError):
    def __init__(self, status_code: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code


def _public_error(exc: CueFlowError) -> JSONResponse:
    headers: dict[str, str] | None = None
    if isinstance(exc, _HttpRequestError):
        status_code, code, message = exc.status_code, exc.code, str(exc)
    elif isinstance(exc, AuthenticationRateLimitedError):
        status_code, code, message = 429, "rate_limited", "Try again later."
        headers = {"Retry-After": str(exc.retry_after_seconds)}
    elif isinstance(exc, PhoneBlockedError):
        status_code, code, message = (
            403,
            "phone_blocked",
            "This phone is blocked. Contact support to appeal.",
        )
    elif isinstance(exc, SmsProviderUnavailableError):
        status_code, code, message = (
            503,
            "sms_provider_unavailable",
            "SMS verification is temporarily unavailable.",
        )
    elif isinstance(exc, PhoneReputationIntegrityError):
        status_code, code, message = (
            503,
            "authentication_temporarily_unavailable",
            "Authentication is temporarily unavailable.",
        )
    elif isinstance(exc, IdentityConflictError):
        status_code, code, message = (
            409,
            "phone_already_registered",
            "This phone is already bound to another account.",
        )
    elif isinstance(exc, (AuthenticationError, AccountError)) and not isinstance(
        exc, AccountMigrationError
    ):
        status_code, code, message = 401, "authentication_failed", "Authentication failed."
    elif isinstance(exc, ContractError):
        status_code, code, message = 400, "invalid_request", "The request is invalid."
    else:
        status_code, code, message = (
            503,
            "authentication_temporarily_unavailable",
            "Authentication is temporarily unavailable.",
        )
    return JSONResponse(
        {"error": {"code": code, "message": message}},
        status_code=status_code,
        headers=headers,
    )


def _map_errors(endpoint: Endpoint) -> Endpoint:
    @wraps(endpoint)
    async def wrapped(request: Request) -> Response:
        try:
            response = await endpoint(request)
        except CueFlowError as exc:
            response = _public_error(exc)
        response.headers["Cache-Control"] = "no-store"
        response.headers["Pragma"] = "no-cache"
        return response

    return wrapped


async def _with_service(factory: ServiceFactory, action: Callable[[AuthService], T]) -> T:
    def invoke() -> T:
        service = factory()
        try:
            return action(service)
        finally:
            service.store.close()

    return await run_in_threadpool(invoke)


def _require_origin(request: Request, config: AuthHttpConfig) -> None:
    if request.headers.get("origin") not in config.allowed_origins:
        raise _HttpRequestError(403, "origin_not_allowed", "The request Origin is not allowed.")


async def _json_body(request: Request, config: AuthHttpConfig) -> dict[str, Any]:
    raw_length = request.headers.get("content-length")
    if raw_length is not None:
        try:
            length = int(raw_length)
        except ValueError as exc:
            raise ContractError("request Content-Length is invalid") from exc
        if length < 0 or length > config.max_json_bytes:
            raise ContractError("request JSON exceeds the size limit")
    body = await request.body()
    if len(body) > config.max_json_bytes:
        raise ContractError("request JSON exceeds the size limit")
    try:
        value = await request.json()
    except (UnicodeDecodeError, ValueError) as exc:
        raise ContractError("request body must be valid JSON") from exc
    if not isinstance(value, dict):
        raise ContractError("request JSON must be an object")
    return cast(dict[str, Any], value)


def _required_string(payload: dict[str, Any], field: str, *, max_length: int = 512) -> str:
    value = payload.get(field)
    if not isinstance(value, str) or not value or len(value) > max_length:
        raise ContractError(f"{field} must be a non-empty string")
    return value


def _required_integer(payload: dict[str, Any], field: str) -> int:
    value = payload.get(field)
    if not isinstance(value, int) or isinstance(value, bool):
        raise ContractError(f"{field} must be an integer")
    return value


def _client_id(request: Request) -> str:
    value = request.headers.get(CLIENT_HEADER)
    if value is None or not value or len(value) > 128:
        raise ContractError(f"{CLIENT_HEADER} is required")
    return value


def _ip_address(request: Request) -> str:
    if request.client is None or not request.client.host:
        raise ContractError("request client address is unavailable")
    return request.client.host


def _access_cookie(request: Request) -> str:
    value = request.cookies.get(ACCESS_COOKIE)
    if not value:
        raise AuthenticationError("access token is missing")
    return value


def _refresh_cookie(request: Request) -> str:
    value = request.cookies.get(REFRESH_COOKIE)
    if not value:
        raise AuthenticationError("refresh token is missing")
    return value


def _require_bound_csrf(request: Request, service: AuthService) -> str:
    raw_access = _access_cookie(request)
    cookie = request.cookies.get(CSRF_COOKIE)
    header = request.headers.get(CSRF_HEADER)
    if (
        not cookie
        or not header
        or not hmac.compare_digest(cookie, header)
        or not service.verify_csrf_token(raw_access, header)
    ):
        raise _HttpRequestError(403, "csrf_failed", "CSRF validation failed.")
    return raw_access


def _grant(payload: dict[str, Any], purpose: SmsPurpose) -> VerificationGrant:
    return VerificationGrant(
        grant_id=_required_string(payload, "grant_id", max_length=128),
        raw_grant=_required_string(payload, "grant_token", max_length=1024),
        purpose=purpose,
        expires_at=_required_integer(payload, "grant_expires_at"),
    )


def _grant_json(grant: VerificationGrant) -> dict[str, Any]:
    return {
        "grant_id": grant.grant_id,
        "grant_token": grant.raw_grant,
        "grant_expires_at": grant.expires_at,
    }


def _session_json(session: AuthenticatedSession) -> dict[str, Any]:
    return {
        "authenticated": True,
        "user_id": session.user_id,
        "session_family_id": session.session_family_id,
        "session_id": session.session_id,
        "access_expires_at": session.access_expires_at,
        "refresh_expires_at": session.refresh_expires_at,
    }


def _set_session_cookies(
    response: Response, session: AuthenticatedSession, service: AuthService
) -> None:
    # The access credential remains logically valid for only access_ttl_ms. Its cookie is retained
    # through the refresh window so the expired credential can still bind refresh CSRF validation.
    browser_window = service.policy.refresh_ttl_ms // 1000
    common: dict[str, Any] = {
        "max_age": browser_window,
        "path": "/",
        "secure": True,
        "samesite": "strict",
    }
    response.set_cookie(
        ACCESS_COOKIE,
        session.access_token,
        httponly=True,
        **common,
    )
    response.set_cookie(
        REFRESH_COOKIE,
        session.refresh_token,
        httponly=True,
        **common,
    )
    response.set_cookie(
        CSRF_COOKIE,
        session.csrf_token,
        httponly=False,
        **common,
    )


def _clear_session_cookies(response: Response) -> None:
    for name, http_only in (
        (ACCESS_COOKIE, True),
        (REFRESH_COOKIE, True),
        (CSRF_COOKIE, False),
    ):
        response.delete_cookie(
            name,
            path="/",
            secure=True,
            httponly=http_only,
            samesite="strict",
        )


def _session_response(
    session: AuthenticatedSession, service: AuthService, *, status_code: int = 200
) -> JSONResponse:
    response = JSONResponse(_session_json(session), status_code=status_code)
    _set_session_cookies(response, session, service)
    return response


def create_auth_app(*, service_factory: ServiceFactory, config: AuthHttpConfig) -> Starlette:
    """Build the v0.6.1 ASGI surface around request-owned AuthService instances."""

    async def payload(request: Request) -> dict[str, Any]:
        _require_origin(request, config)
        return await _json_body(request, config)

    @_map_errors
    async def request_phone_continue(request: Request) -> Response:
        body = await payload(request)
        receipt = await _with_service(
            service_factory,
            lambda service: service.request_phone_code(
                _required_string(body, "phone", max_length=32),
                purpose=SmsPurpose.PHONE_CONTINUE,
                client_id=_client_id(request),
                ip_address=_ip_address(request),
            ),
        )
        return JSONResponse(
            {"challenge_id": receipt.challenge_id, "expires_at": receipt.expires_at},
            status_code=202,
        )

    @_map_errors
    async def verify_phone_continue(request: Request) -> Response:
        body = await payload(request)

        def action(service: AuthService) -> Response:
            result = service.verify_phone_code(
                challenge_id=_required_string(body, "challenge_id", max_length=128),
                phone=_required_string(body, "phone", max_length=32),
                code=_required_string(body, "code", max_length=16),
                purpose=SmsPurpose.PHONE_CONTINUE,
            )
            if not isinstance(result, PhoneContinueResult):
                raise AssertionError("phone continue returned an unexpected result")
            if result.kind is PhoneContinueKind.AUTHENTICATED:
                assert result.session is not None
                return _session_response(result.session, service)
            assert result.registration_grant is not None
            return JSONResponse(
                {
                    "authenticated": False,
                    "registration_required": True,
                    **_grant_json(result.registration_grant),
                }
            )

        return await _with_service(service_factory, action)

    @_map_errors
    async def complete_registration(request: Request) -> Response:
        body = await payload(request)

        def action(service: AuthService) -> Response:
            session = service.complete_registration(
                phone=_required_string(body, "phone", max_length=32),
                grant=_grant(body, SmsPurpose.PHONE_CONTINUE),
                password=_required_string(body, "password", max_length=256),
            )
            return _session_response(session, service, status_code=201)

        return await _with_service(service_factory, action)

    @_map_errors
    async def cancel_registration(request: Request) -> Response:
        body = await payload(request)
        await _with_service(
            service_factory,
            lambda service: service.cancel_registration(_grant(body, SmsPurpose.PHONE_CONTINUE)),
        )
        return Response(status_code=204)

    @_map_errors
    async def password_login(request: Request) -> Response:
        body = await payload(request)

        def action(service: AuthService) -> Response:
            session = service.login_with_password(
                phone=_required_string(body, "phone", max_length=32),
                password=_required_string(body, "password", max_length=256),
                client_id=_client_id(request),
                ip_address=_ip_address(request),
            )
            return _session_response(session, service)

        return await _with_service(service_factory, action)

    def request_code_endpoint(purpose: SmsPurpose) -> Endpoint:
        @_map_errors
        async def endpoint(request: Request) -> Response:
            body = await payload(request)
            receipt = await _with_service(
                service_factory,
                lambda service: service.request_phone_code(
                    _required_string(body, "phone", max_length=32),
                    purpose=purpose,
                    client_id=_client_id(request),
                    ip_address=_ip_address(request),
                ),
            )
            return JSONResponse(
                {"challenge_id": receipt.challenge_id, "expires_at": receipt.expires_at},
                status_code=202,
            )

        return endpoint

    def verify_code_endpoint(purpose: SmsPurpose, *, csrf: bool = False) -> Endpoint:
        @_map_errors
        async def endpoint(request: Request) -> Response:
            body = await payload(request)

            def action(service: AuthService) -> Response:
                if csrf:
                    raw_access = _require_bound_csrf(request, service)
                    service.authenticate_access_token(raw_access)
                result = service.verify_phone_code(
                    challenge_id=_required_string(body, "challenge_id", max_length=128),
                    phone=_required_string(body, "phone", max_length=32),
                    code=_required_string(body, "code", max_length=16),
                    purpose=purpose,
                )
                if not isinstance(result, VerificationGrant):
                    raise AssertionError("verification route returned an unexpected result")
                return JSONResponse(_grant_json(result))

            return await _with_service(service_factory, action)

        return endpoint

    @_map_errors
    async def reset_password(request: Request) -> Response:
        body = await payload(request)
        await _with_service(
            service_factory,
            lambda service: service.reset_password(
                phone=_required_string(body, "phone", max_length=32),
                grant=_grant(body, SmsPurpose.PASSWORD_RESET),
                new_password=_required_string(body, "new_password", max_length=256),
            ),
        )
        response = Response(status_code=204)
        _clear_session_cookies(response)
        return response

    @_map_errors
    async def change_password(request: Request) -> Response:
        body = await payload(request)

        def action(service: AuthService) -> Response:
            raw_access = _require_bound_csrf(request, service)
            session = service.change_password(
                raw_access_token=raw_access,
                current_password=_required_string(body, "current_password", max_length=256),
                new_password=_required_string(body, "new_password", max_length=256),
            )
            return _session_response(session, service)

        return await _with_service(service_factory, action)

    @_map_errors
    async def request_phone_change(request: Request) -> Response:
        body = await payload(request)

        def action(service: AuthService) -> Response:
            raw_access = _require_bound_csrf(request, service)
            receipt = service.request_phone_change_code(
                raw_access_token=raw_access,
                current_password=_required_string(body, "current_password", max_length=256),
                new_phone=_required_string(body, "new_phone", max_length=32),
                client_id=_client_id(request),
                ip_address=_ip_address(request),
            )
            return JSONResponse(
                {"challenge_id": receipt.challenge_id, "expires_at": receipt.expires_at},
                status_code=202,
            )

        return await _with_service(service_factory, action)

    @_map_errors
    async def complete_phone_change(request: Request) -> Response:
        body = await payload(request)

        def action(service: AuthService) -> Response:
            raw_access = _require_bound_csrf(request, service)
            session = service.complete_phone_change(
                raw_access_token=raw_access,
                new_phone=_required_string(body, "new_phone", max_length=32),
                grant=_grant(body, SmsPurpose.PHONE_CHANGE),
            )
            return _session_response(session, service)

        return await _with_service(service_factory, action)

    @_map_errors
    async def refresh_session(request: Request) -> Response:
        _require_origin(request, config)

        def action(service: AuthService) -> Response:
            _require_bound_csrf(request, service)
            session = service.refresh_session(_refresh_cookie(request))
            return _session_response(session, service)

        return await _with_service(service_factory, action)

    @_map_errors
    async def logout_current(request: Request) -> Response:
        _require_origin(request, config)

        def action(service: AuthService) -> None:
            service.logout_current_session(_require_bound_csrf(request, service))

        await _with_service(service_factory, action)
        response = Response(status_code=204)
        _clear_session_cookies(response)
        return response

    @_map_errors
    async def logout_all(request: Request) -> Response:
        _require_origin(request, config)

        def action(service: AuthService) -> None:
            service.logout_all_sessions(_require_bound_csrf(request, service))

        await _with_service(service_factory, action)
        response = Response(status_code=204)
        _clear_session_cookies(response)
        return response

    @_map_errors
    async def request_account_erasure(request: Request) -> Response:
        body = await payload(request)
        receipt = await _with_service(
            service_factory,
            lambda service: service.request_account_erasure_code(
                phone=_required_string(body, "phone", max_length=32),
                client_id=_client_id(request),
                ip_address=_ip_address(request),
            ),
        )
        return JSONResponse(
            {"challenge_id": receipt.challenge_id, "expires_at": receipt.expires_at},
            status_code=202,
        )

    @_map_errors
    async def complete_account_erasure(request: Request) -> Response:
        body = await payload(request)
        await _with_service(
            service_factory,
            lambda service: service.erase_qualifying_banned_account(
                phone=_required_string(body, "phone", max_length=32),
                grant=_grant(body, SmsPurpose.ACCOUNT_ERASURE),
            ),
        )
        response = Response(status_code=204)
        _clear_session_cookies(response)
        return response

    routes = [
        Route("/auth/phone/continue/request-code", request_phone_continue, methods=["POST"]),
        Route("/auth/phone/continue/verify-code", verify_phone_continue, methods=["POST"]),
        Route("/auth/registration/complete", complete_registration, methods=["POST"]),
        Route("/auth/registration/cancel", cancel_registration, methods=["POST"]),
        Route("/auth/password/login", password_login, methods=["POST"]),
        Route(
            "/auth/password/reset/request-code",
            request_code_endpoint(SmsPurpose.PASSWORD_RESET),
            methods=["POST"],
        ),
        Route(
            "/auth/password/reset/verify-code",
            verify_code_endpoint(SmsPurpose.PASSWORD_RESET),
            methods=["POST"],
        ),
        Route("/auth/password/reset/complete", reset_password, methods=["POST"]),
        Route("/auth/password/change", change_password, methods=["POST"]),
        Route("/auth/phone/change/request-code", request_phone_change, methods=["POST"]),
        Route(
            "/auth/phone/change/verify-code",
            verify_code_endpoint(SmsPurpose.PHONE_CHANGE, csrf=True),
            methods=["POST"],
        ),
        Route("/auth/phone/change/complete", complete_phone_change, methods=["POST"]),
        Route("/auth/session/refresh", refresh_session, methods=["POST"]),
        Route("/auth/session/logout", logout_current, methods=["POST"]),
        Route("/auth/session/logout-all", logout_all, methods=["POST"]),
        Route("/auth/account/erasure/request-code", request_account_erasure, methods=["POST"]),
        Route(
            "/auth/account/erasure/verify-code",
            verify_code_endpoint(SmsPurpose.ACCOUNT_ERASURE),
            methods=["POST"],
        ),
        Route("/auth/account/erasure/complete", complete_account_erasure, methods=["POST"]),
    ]
    return Starlette(debug=False, routes=routes)
