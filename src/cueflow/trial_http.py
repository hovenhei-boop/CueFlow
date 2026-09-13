from __future__ import annotations

import asyncio
import hashlib
import hmac
import ipaddress
import json
import shutil
import uuid
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from importlib import resources
from pathlib import Path
from typing import Any

from starlette.applications import Starlette
from starlette.datastructures import FormData, UploadFile
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, Response
from starlette.routing import Route

from cueflow.config import MAX_USER_KEYWORDS, TrialConfig
from cueflow.errors import ContractError, TrialAdmissionError, TrialNotFoundError
from cueflow.trial_service import TrialService
from cueflow.trial_storage import safe_job_workspace

VISITOR_COOKIE = "__Host-cueflow_trial_visitor"
FINGERPRINT_HEADER = "X-CueFlow-Fingerprint"
MAX_REFERENCE_FILES = 20
MAX_REFERENCE_BYTES = 100_000_000


class TrialHttpError(Exception):
    def __init__(self, status_code: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code


def create_trial_app(*, service: TrialService, config: TrialConfig) -> Starlette:
    _validate_http_config(config)

    @asynccontextmanager
    async def lifespan(_: Starlette) -> AsyncIterator[None]:
        service.sweep()
        service.refresh_storage_readiness()
        stop = asyncio.Event()
        task = asyncio.create_task(_periodic_sweep(service, stop))
        try:
            yield
        finally:
            stop.set()
            await task
            service.close()

    async def index(_: Request) -> Response:
        return _static("index.html", "text/html; charset=utf-8")

    async def admin_page(_: Request) -> Response:
        return _static("admin.html", "text/html; charset=utf-8")

    async def static_asset(request: Request) -> Response:
        name = str(request.path_params["name"])
        if name not in {"styles.css", "app.js", "admin.js"}:
            return Response(status_code=404)
        content_type = "text/css; charset=utf-8" if name.endswith(".css") else (
            "text/javascript; charset=utf-8"
        )
        return _static(name, content_type)

    async def heartbeat(request: Request) -> Response:
        _require_origin(request, config)
        visitor_id, created = _visitor(request)
        value = service.heartbeat(
            visitor_id, fingerprint_hmac=_fingerprint_hmac(request, config)
        )
        return _api(value, visitor_id=visitor_id if created else None)

    async def create_job(request: Request) -> Response:
        _require_origin(request, config)
        visitor_id, created = _visitor(request)
        job_id = "job_" + uuid.uuid4().hex
        request_id = "req_" + uuid.uuid4().hex
        workspace = safe_job_workspace(config.work_root, job_id)
        workspace.mkdir(parents=True, exist_ok=False)
        try:
            form = await request.form(
                max_files=MAX_REFERENCE_FILES + 1,
                max_fields=MAX_USER_KEYWORDS + 5,
                max_part_size=config.max_source_bytes,
            )
            media, reference_uploads, keywords = _form_inputs(form)
            upload_root = workspace / "uploads"
            media_path = await _save_upload(
                media,
                upload_root / ("media" + _safe_suffix(media.filename)),
                limit=config.max_source_bytes,
            )
            references: list[Path] = []
            total_reference_bytes = 0
            for index, upload in enumerate(reference_uploads):
                target = upload_root / f"reference-{index:02d}{_safe_suffix(upload.filename)}"
                saved = await _save_upload(
                    upload, target, limit=MAX_REFERENCE_BYTES - total_reference_bytes
                )
                total_reference_bytes += saved.stat().st_size
                references.append(saved)
            value = service.submit(
                job_id=job_id,
                request_id=request_id,
                visitor_id=visitor_id,
                ip_hmac=_ip_hmac(request, config),
                fingerprint_hmac=_fingerprint_hmac(request, config),
                media_path=media_path,
                references=references,
                keywords=keywords,
            )
        except BaseException:
            if workspace.is_dir():
                shutil.rmtree(workspace)
            raise
        return _api(value, status_code=202, visitor_id=visitor_id if created else None)

    async def list_jobs(request: Request) -> Response:
        visitor_id, created = _visitor(request)
        return _api(
            {"jobs": service.jobs(visitor_id)},
            visitor_id=visitor_id if created else None,
        )

    async def get_job(request: Request) -> Response:
        visitor_id, created = _visitor(request)
        value = service.job(str(request.path_params["job_id"]), visitor_id)
        return _api(value, visitor_id=visitor_id if created else None)

    async def retry_job(request: Request) -> Response:
        return await _retry_or_resume(request, "retry")

    async def resume_job(request: Request) -> Response:
        return await _retry_or_resume(request, "resume")

    async def _retry_or_resume(request: Request, action: str) -> Response:
        _require_origin(request, config)
        visitor_id, created = _visitor(request)
        value = service.retry_or_resume(
            job_id=str(request.path_params["job_id"]),
            action_kind=action,
            visitor_id=visitor_id,
            ip_hmac=_ip_hmac(request, config),
            fingerprint_hmac=_fingerprint_hmac(request, config),
        )
        return _api(value, status_code=202, visitor_id=visitor_id if created else None)

    async def result(request: Request) -> Response:
        visitor_id, _ = _visitor(request)
        url = service.result_url(str(request.path_params["job_id"]), visitor_id)
        response = RedirectResponse(url, status_code=302)
        response.headers["Cache-Control"] = "no-store"
        return response

    async def admin_summary(request: Request) -> Response:
        _require_operator(request, config)
        return _api(service.summary())

    async def admin_requests(request: Request) -> Response:
        _require_operator(request, config)
        raw_limit = request.query_params.get("limit", "100")
        try:
            limit = int(raw_limit)
        except ValueError as exc:
            raise TrialHttpError(400, "invalid_limit", "limit must be an integer") from exc
        return _api({"requests": service.list_requests(limit=limit)})

    async def pause(request: Request) -> Response:
        return await _control(request, "pause")

    async def resume(request: Request) -> Response:
        return await _control(request, "resume")

    async def set_budget(request: Request) -> Response:
        _require_origin(request, config)
        _require_operator(request, config)
        body = await _json_body(request)
        micros = body.get("daily_budget_micros")
        if not isinstance(micros, int) or micros <= 0:
            raise TrialHttpError(
                400, "invalid_budget", "daily_budget_micros must be a positive integer"
            )
        reason = _reason(body)
        return _api(service.append_control(
            "set_daily_budget_override", reason=reason, daily_budget_micros=micros
        ))

    async def clear_budget(request: Request) -> Response:
        _require_origin(request, config)
        _require_operator(request, config)
        body = await _json_body(request)
        return _api(service.append_control(
            "clear_daily_budget_override", reason=_reason(body)
        ))

    async def _control(request: Request, action: str) -> Response:
        _require_origin(request, config)
        _require_operator(request, config)
        body = await _json_body(request)
        return _api(service.append_control(action, reason=_reason(body)))

    async def http_error(_: Request, exc: Exception) -> Response:
        assert isinstance(exc, TrialHttpError)
        return _api(
            {"error": {"code": exc.code, "message": str(exc)}},
            status_code=exc.status_code,
        )

    async def admission_error(_: Request, exc: Exception) -> Response:
        assert isinstance(exc, TrialAdmissionError)
        return _api(
            {"error": {"code": exc.reason, "message": str(exc)}},
            status_code=exc.status_code,
        )

    async def not_found(_: Request, exc: Exception) -> Response:
        del exc
        return _api(
            {"error": {"code": "not_found", "message": "The Trial resource was not found."}},
            status_code=404,
        )

    async def contract_error(_: Request, exc: Exception) -> Response:
        return _api(
            {"error": {"code": "invalid_request", "message": str(exc)}}, status_code=400
        )

    return Starlette(
        debug=False,
        lifespan=lifespan,
        routes=[
            Route("/trial", index, methods=["GET"]),
            Route("/trial/admin", admin_page, methods=["GET"]),
            Route("/trial/static/{name}", static_asset, methods=["GET"]),
            Route("/trial/heartbeat", heartbeat, methods=["POST"]),
            Route("/trial/jobs", create_job, methods=["POST"]),
            Route("/trial/jobs", list_jobs, methods=["GET"]),
            Route("/trial/jobs/{job_id}", get_job, methods=["GET"]),
            Route("/trial/jobs/{job_id}/result", result, methods=["GET"]),
            Route("/trial/jobs/{job_id}/retry", retry_job, methods=["POST"]),
            Route("/trial/jobs/{job_id}/resume", resume_job, methods=["POST"]),
            Route("/trial/admin/summary", admin_summary, methods=["GET"]),
            Route("/trial/admin/requests", admin_requests, methods=["GET"]),
            Route("/trial/admin/pause", pause, methods=["POST"]),
            Route("/trial/admin/resume", resume, methods=["POST"]),
            Route("/trial/admin/daily-budget-override", set_budget, methods=["POST"]),
            Route("/trial/admin/daily-budget-override", clear_budget, methods=["DELETE"]),
        ],
        exception_handlers={
            TrialHttpError: http_error,
            TrialAdmissionError: admission_error,
            TrialNotFoundError: not_found,
            ContractError: contract_error,
        },
    )


async def _periodic_sweep(service: TrialService, stop: asyncio.Event) -> None:
    while True:
        try:
            await asyncio.wait_for(stop.wait(), timeout=service.config.stale_sweep_seconds)
            return
        except TimeoutError:
            await asyncio.to_thread(service.sweep)


def _validate_http_config(config: TrialConfig) -> None:
    if not config.public_origin.startswith("https://") or config.public_origin.endswith("/"):
        raise ContractError("TRIAL_PUBLIC_ORIGIN must be one exact HTTPS origin")
    if min(
        len(config.ip_hmac_secret),
        len(config.fingerprint_hmac_secret),
        len(config.operator_secret),
    ) < 32:
        raise ContractError("Trial secrets must each contain at least 32 characters")


def _visitor(request: Request) -> tuple[str, bool]:
    existing = request.cookies.get(VISITOR_COOKIE, "")
    if existing.startswith("v_") and len(existing) == 34 and all(
        character in "0123456789abcdef" for character in existing[2:]
    ):
        return existing, False
    return "v_" + uuid.uuid4().hex, True


def _fingerprint_hmac(request: Request, config: TrialConfig) -> str | None:
    value = request.headers.get(FINGERPRINT_HEADER, "").strip()
    if not value or len(value.encode("utf-8")) > 2048:
        return None
    return _hmac(config.fingerprint_hmac_secret, value)


def _ip_hmac(request: Request, config: TrialConfig) -> str:
    if request.client is None:
        normalized = "unknown"
    else:
        try:
            normalized = ipaddress.ip_address(request.client.host).compressed
        except ValueError:
            normalized = request.client.host.strip().lower()
    return _hmac(config.ip_hmac_secret, normalized)


def _hmac(secret: str, value: str) -> str:
    return "hmac-sha256:" + hmac.new(
        secret.encode("utf-8"), value.encode("utf-8"), hashlib.sha256
    ).hexdigest()


def _require_origin(request: Request, config: TrialConfig) -> None:
    if request.headers.get("origin") != config.public_origin:
        raise TrialHttpError(403, "origin_not_allowed", "The request Origin is not allowed.")


def _require_operator(request: Request, config: TrialConfig) -> None:
    authorization = request.headers.get("authorization", "")
    expected = "Bearer " + config.operator_secret
    if not hmac.compare_digest(authorization.encode(), expected.encode()):
        raise TrialHttpError(401, "operator_required", "A valid operator bearer is required.")


def _api(
    value: Mapping[str, Any],
    *,
    status_code: int = 200,
    visitor_id: str | None = None,
) -> JSONResponse:
    response = JSONResponse(dict(value), status_code=status_code)
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    if visitor_id is not None:
        response.set_cookie(
            VISITOR_COOKIE,
            visitor_id,
            max_age=365 * 24 * 60 * 60,
            path="/",
            secure=True,
            httponly=True,
            samesite="lax",
        )
    return response


def _static(name: str, content_type: str) -> Response:
    item = resources.files("cueflow").joinpath("static").joinpath("trial").joinpath(name)
    response = Response(item.read_bytes(), media_type=content_type)
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
        "connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'"
    )
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


def _form_inputs(form: FormData) -> tuple[UploadFile, list[UploadFile], list[str]]:
    media = form.get("media")
    if not isinstance(media, UploadFile):
        raise TrialHttpError(400, "media_required", "A media file is required.")
    references = [item for item in form.getlist("references") if isinstance(item, UploadFile)]
    if len(references) > MAX_REFERENCE_FILES:
        raise TrialHttpError(400, "too_many_references", "At most 20 references are allowed.")
    keywords = [
        item.strip() for item in form.getlist("keywords")
        if isinstance(item, str) and item.strip()
    ]
    if len(keywords) > MAX_USER_KEYWORDS:
        raise TrialHttpError(400, "too_many_keywords", "Too many keywords were supplied.")
    return media, references, keywords


async def _save_upload(upload: UploadFile, target: Path, *, limit: int) -> Path:
    if limit <= 0:
        raise TrialHttpError(400, "upload_too_large", "Uploaded files are too large.")
    target.parent.mkdir(parents=True, exist_ok=True)
    size = 0
    try:
        with target.open("xb") as stream:
            while block := await upload.read(1024 * 1024):
                size += len(block)
                if size >= limit:
                    raise TrialHttpError(
                        400, "upload_too_large", "Uploaded files exceed the Trial limit."
                    )
                stream.write(block)
    finally:
        await upload.close()
    if size == 0:
        target.unlink(missing_ok=True)
        raise TrialHttpError(400, "empty_upload", "Uploaded files must not be empty.")
    return target


def _safe_suffix(filename: str | None) -> str:
    suffix = Path(filename or "").suffix.lower()
    allowed = ".abcdefghijklmnopqrstuvwxyz0123456789"
    if len(suffix) > 12 or any(character not in allowed for character in suffix):
        return ""
    return suffix


async def _json_body(request: Request) -> Mapping[str, Any]:
    try:
        value = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise TrialHttpError(400, "invalid_json", "A JSON object is required.") from exc
    if not isinstance(value, Mapping):
        raise TrialHttpError(400, "invalid_json", "A JSON object is required.")
    return value


def _reason(body: Mapping[str, Any]) -> str:
    reason = body.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        raise TrialHttpError(400, "reason_required", "A non-empty reason is required.")
    return reason.strip()
