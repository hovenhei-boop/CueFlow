from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

from starlette.testclient import TestClient

from cueflow.auth_http import (
    ACCESS_COOKIE,
    CLIENT_HEADER,
    CSRF_COOKIE,
    CSRF_HEADER,
    REFRESH_COOKIE,
    AuthHttpConfig,
    create_auth_app,
)
from cueflow.phone_reputation import SanctionActor
from tests.account_helpers import (
    AuthStack,
    make_auth_stack,
    make_request_auth_service_factory,
    make_test_account,
)

ORIGIN = "https://cueflow.test"
BASE_HEADERS = {"Origin": ORIGIN, CLIENT_HEADER: "browser-test-client"}
PASSWORD = "correct horse battery staple"


def _client(stack: AuthStack) -> TestClient:
    app = create_auth_app(
        service_factory=make_request_auth_service_factory(stack),
        config=AuthHttpConfig(frozenset({ORIGIN})),
    )
    return TestClient(app, base_url=ORIGIN)


def _csrf_headers(client: TestClient) -> dict[str, str]:
    return {**BASE_HEADERS, CSRF_HEADER: client.cookies.get(CSRF_COOKIE)}


def _register(client: TestClient, stack: AuthStack, phone: str, *, password: str = PASSWORD) -> Any:
    requested = client.post(
        "/auth/phone/continue/request-code",
        json={"phone": phone},
        headers=BASE_HEADERS,
    )
    assert requested.status_code == 202
    verified = client.post(
        "/auth/phone/continue/verify-code",
        json={
            "phone": phone,
            "challenge_id": requested.json()["challenge_id"],
            "code": stack.sms.last_code(),
        },
        headers=BASE_HEADERS,
    )
    assert verified.status_code == 200
    grant = verified.json()
    completed = client.post(
        "/auth/registration/complete",
        json={"phone": phone, "password": password, **grant},
        headers=BASE_HEADERS,
    )
    assert completed.status_code == 201
    return completed


def test_registration_sets_only_host_secure_strict_cookie_session(tmp_path: Path) -> None:
    stack = make_auth_stack(tmp_path)
    try:
        with _client(stack) as client:
            response = _register(client, stack, "+8613810000300")
            body = response.json()
            assert response.headers["cache-control"] == "no-store"
            assert body["authenticated"] is True
            assert "access_token" not in body
            assert "refresh_token" not in body
            assert "csrf_token" not in body

            cookie_headers = response.headers.get_list("set-cookie")
            for name in (ACCESS_COOKIE, REFRESH_COOKIE):
                header = next(value for value in cookie_headers if value.startswith(name + "="))
                assert "Path=/" in header
                assert "Secure" in header
                assert "HttpOnly" in header
                assert "SameSite=strict" in header
                assert "Domain=" not in header
            csrf_header = next(
                value for value in cookie_headers if value.startswith(CSRF_COOKIE + "=")
            )
            assert "Secure" in csrf_header
            assert "HttpOnly" not in csrf_header
            assert "SameSite=strict" in csrf_header

            assert stack.store.connection.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 1
            assert (
                stack.store.connection.execute(
                    "SELECT COUNT(*) FROM password_credentials"
                ).fetchone()[0]
                == 1
            )
            assert (
                stack.store.connection.execute("SELECT COUNT(*) FROM phone_reputations").fetchone()[
                    0
                ]
                == 1
            )
    finally:
        stack.store.close()


def test_existing_phone_continue_verification_logs_in_without_registration(
    tmp_path: Path,
) -> None:
    stack = make_auth_stack(tmp_path)
    phone = "+8613810000313"
    user = make_test_account(stack, phone, password=PASSWORD)
    try:
        with _client(stack) as client:
            requested = client.post(
                "/auth/phone/continue/request-code",
                json={"phone": phone},
                headers=BASE_HEADERS,
            )
            verified = client.post(
                "/auth/phone/continue/verify-code",
                json={
                    "phone": phone,
                    "challenge_id": requested.json()["challenge_id"],
                    "code": stack.sms.last_code(),
                },
                headers=BASE_HEADERS,
            )
            assert verified.status_code == 200
            assert verified.json()["user_id"] == user.user_id
            assert verified.json()["authenticated"] is True
            assert verified.json().get("registration_required") is None
            assert client.cookies.get(ACCESS_COOKIE)
    finally:
        stack.store.close()


def test_registration_grant_expiry_field_is_authenticated(tmp_path: Path) -> None:
    stack = make_auth_stack(tmp_path)
    phone = "+8613810000314"
    try:
        with _client(stack) as client:
            requested = client.post(
                "/auth/phone/continue/request-code",
                json={"phone": phone},
                headers=BASE_HEADERS,
            )
            verified = client.post(
                "/auth/phone/continue/verify-code",
                json={
                    "phone": phone,
                    "challenge_id": requested.json()["challenge_id"],
                    "code": stack.sms.last_code(),
                },
                headers=BASE_HEADERS,
            )
            tampered_grant = verified.json()
            tampered_grant["grant_expires_at"] += 1
            rejected = client.post(
                "/auth/registration/complete",
                json={"phone": phone, "password": PASSWORD, **tampered_grant},
                headers=BASE_HEADERS,
            )
            assert rejected.status_code == 401
            assert rejected.json()["error"]["code"] == "authentication_failed"
            assert stack.store.connection.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0
    finally:
        stack.store.close()


def test_origin_and_csrf_are_fail_closed(tmp_path: Path) -> None:
    stack = make_auth_stack(tmp_path)
    phone = "+8613810000301"
    make_test_account(stack, phone, password=PASSWORD)
    try:
        with _client(stack) as client:
            missing_origin = client.post(
                "/auth/password/login",
                json={"phone": phone, "password": PASSWORD},
                headers={CLIENT_HEADER: "browser-test-client"},
            )
            assert missing_origin.status_code == 403
            assert missing_origin.json()["error"]["code"] == "origin_not_allowed"

            login = client.post(
                "/auth/password/login",
                json={"phone": phone, "password": PASSWORD},
                headers=BASE_HEADERS,
            )
            assert login.status_code == 200
            missing_csrf = client.post(
                "/auth/password/change",
                json={"current_password": PASSWORD, "new_password": "another valid passphrase"},
                headers=BASE_HEADERS,
            )
            assert missing_csrf.status_code == 403
            assert missing_csrf.json()["error"]["code"] == "csrf_failed"

            changed = client.post(
                "/auth/password/change",
                json={"current_password": PASSWORD, "new_password": "another valid passphrase"},
                headers=_csrf_headers(client),
            )
            assert changed.status_code == 200
            logged_out = client.post(
                "/auth/session/logout",
                headers=_csrf_headers(client),
            )
            assert logged_out.status_code == 204
            assert client.cookies.get(ACCESS_COOKIE) is None
            assert client.cookies.get(REFRESH_COOKIE) is None
    finally:
        stack.store.close()


def test_public_password_errors_do_not_reveal_registration_state(tmp_path: Path) -> None:
    stack = make_auth_stack(tmp_path)
    phone = "+8613810000302"
    make_test_account(stack, phone, password=PASSWORD)
    try:
        with _client(stack) as client:
            wrong = client.post(
                "/auth/password/login",
                json={"phone": phone, "password": "this is the wrong password"},
                headers=BASE_HEADERS,
            )
            unknown = client.post(
                "/auth/password/login",
                json={"phone": "+8613810000399", "password": "this is the wrong password"},
                headers=BASE_HEADERS,
            )
            assert wrong.status_code == unknown.status_code == 401
            assert (
                wrong.json()
                == unknown.json()
                == {"error": {"code": "authentication_failed", "message": "Authentication failed."}}
            )
    finally:
        stack.store.close()


def test_blocked_phone_is_explicit_403_and_sends_no_sms(tmp_path: Path) -> None:
    stack = make_auth_stack(tmp_path)
    phone = "+8613810000303"
    make_test_account(stack, phone, password=PASSWORD)
    stack.reputations.administrative_block_phone(
        phone,
        operation_id="http-block-phone",
        actor=SanctionActor("admin", "http-test-admin"),
        reason_code="manual_abuse_block",
        now=stack.clock(),
    )
    try:
        with _client(stack) as client:
            response = client.post(
                "/auth/phone/continue/request-code",
                json={"phone": phone},
                headers=BASE_HEADERS,
            )
            assert response.status_code == 403
            assert response.json()["error"]["code"] == "phone_blocked"
            assert stack.sms.sent == []
    finally:
        stack.store.close()


def test_sms_provider_and_rate_limit_http_mapping(tmp_path: Path) -> None:
    stack = make_auth_stack(tmp_path)
    try:
        with _client(stack) as client:
            stack.sms.fail = True
            unavailable = client.post(
                "/auth/phone/continue/request-code",
                json={"phone": "+8613810000304"},
                headers=BASE_HEADERS,
            )
            assert unavailable.status_code == 503
            assert unavailable.json()["error"]["code"] == "sms_provider_unavailable"

            stack.sms.fail = False
            stack.auth.policy = replace(stack.auth.policy, sms_phone_limit=1)
            first = client.post(
                "/auth/phone/continue/request-code",
                json={"phone": "+8613810000305"},
                headers=BASE_HEADERS,
            )
            second = client.post(
                "/auth/phone/continue/request-code",
                json={"phone": "+8613810000305"},
                headers=BASE_HEADERS,
            )
            assert first.status_code == 202
            assert second.status_code == 429
            assert second.json()["error"]["code"] == "rate_limited"
            assert int(second.headers["retry-after"]) > 0
    finally:
        stack.store.close()


def test_refresh_rotates_all_browser_credentials(tmp_path: Path) -> None:
    stack = make_auth_stack(tmp_path)
    phone = "+8613810000306"
    make_test_account(stack, phone, password=PASSWORD)
    try:
        with _client(stack) as client:
            login = client.post(
                "/auth/password/login",
                json={"phone": phone, "password": PASSWORD},
                headers=BASE_HEADERS,
            )
            assert login.status_code == 200
            before = tuple(
                client.cookies.get(name) for name in (ACCESS_COOKIE, REFRESH_COOKIE, CSRF_COOKIE)
            )
            refreshed = client.post(
                "/auth/session/refresh",
                headers=_csrf_headers(client),
            )
            assert refreshed.status_code == 200
            after = tuple(
                client.cookies.get(name) for name in (ACCESS_COOKIE, REFRESH_COOKIE, CSRF_COOKIE)
            )
            assert all(
                before_value != after_value
                for before_value, after_value in zip(before, after, strict=True)
            )
    finally:
        stack.store.close()


def test_password_reset_revokes_sessions_without_auto_login(tmp_path: Path) -> None:
    stack = make_auth_stack(tmp_path)
    phone = "+8613810000307"
    make_test_account(stack, phone, password=PASSWORD)
    try:
        with _client(stack) as client:
            client.post(
                "/auth/password/login",
                json={"phone": phone, "password": PASSWORD},
                headers=BASE_HEADERS,
            )
            requested = client.post(
                "/auth/password/reset/request-code",
                json={"phone": phone},
                headers=BASE_HEADERS,
            )
            verified = client.post(
                "/auth/password/reset/verify-code",
                json={
                    "phone": phone,
                    "challenge_id": requested.json()["challenge_id"],
                    "code": stack.sms.last_code(),
                },
                headers=BASE_HEADERS,
            )
            reset = client.post(
                "/auth/password/reset/complete",
                json={"phone": phone, "new_password": "new reset passphrase", **verified.json()},
                headers=BASE_HEADERS,
            )
            assert reset.status_code == 204
            assert client.cookies.get(ACCESS_COOKIE) is None
            assert client.cookies.get(REFRESH_COOKIE) is None

            new_login = client.post(
                "/auth/password/login",
                json={"phone": phone, "password": "new reset passphrase"},
                headers=BASE_HEADERS,
            )
            assert new_login.status_code == 200
    finally:
        stack.store.close()


def test_phone_change_is_step_up_verified_and_atomic(tmp_path: Path) -> None:
    stack = make_auth_stack(tmp_path)
    old_phone = "+8613810000308"
    new_phone = "+8613810000309"
    user = make_test_account(stack, old_phone, password=PASSWORD)
    try:
        with _client(stack) as client:
            client.post(
                "/auth/password/login",
                json={"phone": old_phone, "password": PASSWORD},
                headers=BASE_HEADERS,
            )
            requested = client.post(
                "/auth/phone/change/request-code",
                json={"new_phone": new_phone, "current_password": PASSWORD},
                headers=_csrf_headers(client),
            )
            assert requested.status_code == 202
            verified = client.post(
                "/auth/phone/change/verify-code",
                json={
                    "phone": new_phone,
                    "challenge_id": requested.json()["challenge_id"],
                    "code": stack.sms.last_code(),
                },
                headers=_csrf_headers(client),
            )
            completed = client.post(
                "/auth/phone/change/complete",
                json={"new_phone": new_phone, **verified.json()},
                headers=_csrf_headers(client),
            )
            assert completed.status_code == 200
            active_phone = stack.store.connection.execute(
                """SELECT provider_subject FROM auth_identities
                WHERE user_id=? AND provider='phone' AND status='active'""",
                (user.user_id,),
            ).fetchone()[0]
            assert active_phone == new_phone
    finally:
        stack.store.close()


def test_phone_change_reports_occupied_phone_after_step_up(tmp_path: Path) -> None:
    stack = make_auth_stack(tmp_path)
    source_phone = "+8613810000310"
    occupied_phone = "+8613810000311"
    make_test_account(stack, source_phone, password=PASSWORD)
    make_test_account(stack, occupied_phone, password=PASSWORD)
    try:
        with _client(stack) as client:
            client.post(
                "/auth/password/login",
                json={"phone": source_phone, "password": PASSWORD},
                headers=BASE_HEADERS,
            )
            response = client.post(
                "/auth/phone/change/request-code",
                json={"new_phone": occupied_phone, "current_password": PASSWORD},
                headers=_csrf_headers(client),
            )
            assert response.status_code == 409
            assert response.json()["error"]["code"] == "phone_already_registered"
    finally:
        stack.store.close()


def test_qualifying_ban_account_erasure_keeps_phone_reputation(tmp_path: Path) -> None:
    stack = make_auth_stack(tmp_path)
    phone = "+8613810000312"
    user = make_test_account(stack, phone, password=PASSWORD)
    reputation = stack.reputations.apply_qualifying_ban(
        user.user_id,
        operation_id="http-qualifying-ban",
        actor=SanctionActor("admin", "http-test-admin"),
        reason_code="formal_account_ban",
        now=stack.clock(),
    )
    try:
        with _client(stack) as client:
            requested = client.post(
                "/auth/account/erasure/request-code",
                json={"phone": phone},
                headers=BASE_HEADERS,
            )
            assert requested.status_code == 202
            verified = client.post(
                "/auth/account/erasure/verify-code",
                json={
                    "phone": phone,
                    "challenge_id": requested.json()["challenge_id"],
                    "code": stack.sms.last_code(),
                },
                headers=BASE_HEADERS,
            )
            erased = client.post(
                "/auth/account/erasure/complete",
                json={"phone": phone, **verified.json()},
                headers=BASE_HEADERS,
            )
            assert erased.status_code == 204
            assert stack.store.connection.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0
            row = stack.store.connection.execute(
                "SELECT qualifying_ban_count FROM phone_reputations WHERE phone_reputation_id=?",
                (reputation.phone_reputation_id,),
            ).fetchone()
            assert row is not None and row[0] == 1
    finally:
        stack.store.close()
