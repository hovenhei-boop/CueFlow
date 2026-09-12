from __future__ import annotations

from pathlib import Path

import pytest

from cueflow.auth import PhoneContinueKind, VerificationGrant
from cueflow.auth_crypto import (
    FAST_TEST_ARGON2_CONFIG,
    PRODUCTION_ARGON2_CONFIG,
    PasswordHashService,
    PasswordVerification,
)
from cueflow.errors import (
    AuthenticationError,
    AuthenticationRateLimitedError,
    ContractError,
    IdentityConflictError,
    PhoneBlockedError,
    SmsProviderUnavailableError,
)
from cueflow.phone_reputation import SanctionActor
from cueflow.sms import SmsPurpose
from tests.account_helpers import Clock, make_auth_stack, make_test_account

PASSWORD = "correct horse battery staple"
NEW_PASSWORD = "a much longer replacement password"


def _registration_grant(stack: object, phone: str) -> VerificationGrant:
    auth = stack.auth  # type: ignore[attr-defined]
    sms = stack.sms  # type: ignore[attr-defined]
    receipt = auth.request_phone_code(
        phone,
        purpose=SmsPurpose.PHONE_CONTINUE,
        client_id="client-1",
        ip_address="203.0.113.1",
    )
    result = auth.verify_phone_code(
        challenge_id=receipt.challenge_id,
        phone=phone,
        code=sms.last_code(),
        purpose=SmsPurpose.PHONE_CONTINUE,
    )
    assert result.kind is PhoneContinueKind.REGISTRATION_REQUIRED
    assert result.registration_grant is not None
    return result.registration_grant


def _purpose_grant(
    stack: object, phone: str, purpose: SmsPurpose, *, user_id: str | None = None
) -> VerificationGrant:
    auth = stack.auth  # type: ignore[attr-defined]
    sms = stack.sms  # type: ignore[attr-defined]
    receipt = auth.request_phone_code(
        phone,
        purpose=purpose,
        client_id="client-purpose",
        ip_address="203.0.113.2",
        user_id=user_id,
    )
    grant = auth.verify_phone_code(
        challenge_id=receipt.challenge_id,
        phone=phone,
        code=sms.last_code(),
        purpose=purpose,
    )
    assert isinstance(grant, VerificationGrant)
    return grant


def test_registration_creates_only_one_complete_account_at_final_commit(tmp_path: Path) -> None:
    stack = make_auth_stack(tmp_path)
    try:
        phone = "+8613810000100"
        grant = _registration_grant(stack, phone)
        assert stack.store.connection.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0
        assert (
            stack.store.connection.execute("SELECT COUNT(*) FROM phone_reputations").fetchone()[0]
            == 0
        )

        session = stack.auth.complete_registration(phone=phone, grant=grant, password=PASSWORD)

        assert stack.auth.authenticate_access_token(session.access_token) == session.user_id
        assert (
            stack.store.connection.execute(
                "SELECT COUNT(*) FROM password_credentials WHERE user_id=?", (session.user_id,)
            ).fetchone()[0]
            == 1
        )
        password_hash = stack.store.connection.execute(
            "SELECT password_hash FROM password_credentials WHERE user_id=?", (session.user_id,)
        ).fetchone()[0]
        assert password_hash.startswith("$argon2id$")
        assert PASSWORD not in password_hash
        identity = stack.store.connection.execute(
            "SELECT * FROM auth_identities WHERE user_id=? AND status='active'",
            (session.user_id,),
        ).fetchone()
        assert identity["phone_reputation_id"] is not None
        assert stack.reputations.find_by_phone(phone).status == "normal"
        with pytest.raises(AuthenticationError, match="invalid or expired"):
            stack.auth.complete_registration(phone=phone, grant=grant, password=PASSWORD)
    finally:
        stack.store.close()


def test_registration_grant_is_bound_to_phone_and_cancel_removes_transient_state(
    tmp_path: Path,
) -> None:
    stack = make_auth_stack(tmp_path)
    try:
        grant = _registration_grant(stack, "+8613810000101")
        with pytest.raises(AuthenticationError, match="invalid or expired"):
            stack.auth.complete_registration(phone="+8613810000102", grant=grant, password=PASSWORD)
        assert stack.store.connection.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0
        stack.auth.cancel_registration(grant)
        assert (
            stack.store.connection.execute(
                "SELECT COUNT(*) FROM phone_verification_grants"
            ).fetchone()[0]
            == 0
        )
        assert (
            stack.store.connection.execute("SELECT COUNT(*) FROM sms_challenges").fetchone()[0] == 0
        )
    finally:
        stack.store.close()


def test_registration_failure_rolls_back_user_credentials_reputation_and_session(
    tmp_path: Path,
) -> None:
    stack = make_auth_stack(tmp_path)
    try:
        phone = "+8613810000109"
        grant = _registration_grant(stack, phone)
        stack.store.connection.execute(
            """CREATE TEMP TRIGGER fail_access_token_insert
            BEFORE INSERT ON access_tokens BEGIN
                SELECT RAISE(ABORT, 'simulated final registration failure');
            END"""
        )
        with pytest.raises(IdentityConflictError):
            stack.auth.complete_registration(phone=phone, grant=grant, password=PASSWORD)
        for table in (
            "users",
            "auth_identities",
            "password_credentials",
            "phone_reputations",
            "session_families",
            "sessions",
            "access_tokens",
        ):
            assert (
                stack.store.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
            )
        assert (
            stack.store.connection.execute(
                "SELECT consumed_at FROM phone_verification_grants WHERE grant_id=?",
                (grant.grant_id,),
            ).fetchone()[0]
            is None
        )
    finally:
        stack.store.close()


def test_existing_phone_continue_and_password_login_share_complete_session_creation(
    tmp_path: Path,
) -> None:
    stack = make_auth_stack(tmp_path)
    try:
        phone = "+8613810000103"
        registered = stack.auth.complete_registration(
            phone=phone, grant=_registration_grant(stack, phone), password=PASSWORD
        )
        receipt = stack.auth.request_phone_code(
            phone,
            purpose=SmsPurpose.PHONE_CONTINUE,
            client_id="client-2",
            ip_address="203.0.113.3",
        )
        continued = stack.auth.verify_phone_code(
            challenge_id=receipt.challenge_id,
            phone=phone,
            code=stack.sms.last_code(),
            purpose=SmsPurpose.PHONE_CONTINUE,
        )
        assert continued.kind is PhoneContinueKind.AUTHENTICATED
        assert continued.session is not None
        assert continued.session.user_id == registered.user_id

        password_session = stack.auth.login_with_password(
            phone=phone,
            password=PASSWORD,
            client_id="client-3",
            ip_address="203.0.113.4",
        )
        assert password_session.user_id == registered.user_id
    finally:
        stack.store.close()


def test_password_failures_are_generic_and_progressively_rate_limited(tmp_path: Path) -> None:
    stack = make_auth_stack(tmp_path)
    try:
        phone = "+8613810000104"
        make_test_account(stack, phone, password=PASSWORD)
        for _ in range(stack.auth.policy.password_phone_limit):
            with pytest.raises(AuthenticationError, match="phone or password"):
                stack.auth.login_with_password(
                    phone=phone,
                    password="wrong password value",
                    client_id="password-client",
                    ip_address="203.0.113.5",
                )
        with pytest.raises(AuthenticationRateLimitedError) as caught:
            stack.auth.login_with_password(
                phone=phone,
                password=PASSWORD,
                client_id="password-client",
                ip_address="203.0.113.5",
            )
        assert caught.value.retry_after_seconds >= 1
        assert (
            stack.store.connection.execute(
                "SELECT COUNT(*) FROM auth_security_events WHERE event_type='password_failure'"
            ).fetchone()[0]
            == stack.auth.policy.password_phone_limit
        )
    finally:
        stack.store.close()


def test_unknown_phone_password_login_executes_dummy_argon_verification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stack = make_auth_stack(tmp_path)
    calls: list[str | None] = []
    original = stack.hasher.verify_password_or_dummy

    def recording_verify(encoded_hash: str | None, password: str) -> PasswordVerification:
        calls.append(encoded_hash)
        return original(encoded_hash, password)

    monkeypatch.setattr(stack.hasher, "verify_password_or_dummy", recording_verify)
    try:
        with pytest.raises(AuthenticationError, match="phone or password"):
            stack.auth.login_with_password(
                phone="+8613810000199",
                password="an unknown password value",
                client_id="unknown-phone-client",
                ip_address="203.0.113.99",
            )
        assert calls == [None]
    finally:
        stack.store.close()


def test_sms_failures_are_persisted_and_requests_are_rate_limited(tmp_path: Path) -> None:
    stack = make_auth_stack(tmp_path)
    try:
        phone = "+8613810000112"
        first = stack.auth.request_phone_code(
            phone,
            purpose=SmsPurpose.PHONE_CONTINUE,
            client_id="sms-client",
            ip_address="203.0.113.9",
        )
        with pytest.raises(AuthenticationError):
            stack.auth.verify_phone_code(
                challenge_id=first.challenge_id,
                phone=phone,
                code="000000" if stack.sms.last_code() != "000000" else "999999",
                purpose=SmsPurpose.PHONE_CONTINUE,
            )
        row = stack.store.connection.execute(
            "SELECT attempts_remaining, status FROM sms_challenges WHERE challenge_id=?",
            (first.challenge_id,),
        ).fetchone()
        assert tuple(row) == (4, "sent")

        for _ in range(stack.auth.policy.sms_phone_limit - 1):
            stack.auth.request_phone_code(
                phone,
                purpose=SmsPurpose.PHONE_CONTINUE,
                client_id="sms-client",
                ip_address="203.0.113.9",
            )
        with pytest.raises(AuthenticationRateLimitedError) as limited:
            stack.auth.request_phone_code(
                phone,
                purpose=SmsPurpose.PHONE_CONTINUE,
                client_id="sms-client",
                ip_address="203.0.113.9",
            )
        assert limited.value.retry_after_seconds >= 1
        assert (
            stack.store.connection.execute(
                "SELECT COUNT(*) FROM auth_security_events WHERE event_type='sms_rate_limited'"
            ).fetchone()[0]
            == 1
        )

        another = "+8613810000113"
        stack.sms.fail = True
        with pytest.raises(SmsProviderUnavailableError):
            stack.auth.request_phone_code(
                another,
                purpose=SmsPurpose.PHONE_CONTINUE,
                client_id="provider-failure-client",
                ip_address="203.0.113.10",
            )
        assert (
            stack.store.connection.execute(
                "SELECT COUNT(*) FROM sms_challenges WHERE status='failed'"
            ).fetchone()[0]
            == 1
        )
    finally:
        stack.store.close()


def test_change_and_reset_password_revoke_old_sessions_with_distinct_reasons(
    tmp_path: Path,
) -> None:
    stack = make_auth_stack(tmp_path)
    try:
        phone = "+8613810000105"
        old = stack.auth.complete_registration(
            phone=phone, grant=_registration_grant(stack, phone), password=PASSWORD
        )
        changed = stack.auth.change_password(
            raw_access_token=old.access_token,
            current_password=PASSWORD,
            new_password=NEW_PASSWORD,
        )
        assert changed.user_id == old.user_id
        assert (
            stack.store.connection.execute(
                "SELECT revocation_reason FROM session_families WHERE session_family_id=?",
                (old.session_family_id,),
            ).fetchone()[0]
            == "password_changed"
        )
        with pytest.raises(AuthenticationError):
            stack.auth.authenticate_access_token(old.access_token)

        reset_grant = _purpose_grant(stack, phone, SmsPurpose.PASSWORD_RESET)
        stack.auth.reset_password(
            phone=phone,
            grant=reset_grant,
            new_password="another secure replacement password",
        )
        assert (
            stack.store.connection.execute(
                "SELECT revocation_reason FROM session_families WHERE session_family_id=?",
                (changed.session_family_id,),
            ).fetchone()[0]
            == "password_reset"
        )
    finally:
        stack.store.close()


def test_phone_change_is_atomic_reuses_long_lived_old_reputation_and_issues_new_session(
    tmp_path: Path,
) -> None:
    stack = make_auth_stack(tmp_path)
    try:
        old_phone = "+8613810000106"
        new_phone = "+8613810000107"
        user = make_test_account(stack, old_phone, password=PASSWORD)
        login = stack.auth.login_with_password(
            phone=old_phone,
            password=PASSWORD,
            client_id="phone-change-login",
            ip_address="203.0.113.6",
        )
        receipt = stack.auth.request_phone_change_code(
            raw_access_token=login.access_token,
            current_password=PASSWORD,
            new_phone=new_phone,
            client_id="phone-change-client",
            ip_address="203.0.113.6",
        )
        grant = stack.auth.verify_phone_code(
            challenge_id=receipt.challenge_id,
            phone=new_phone,
            code=stack.sms.last_code(),
            purpose=SmsPurpose.PHONE_CHANGE,
        )
        assert isinstance(grant, VerificationGrant)
        session = stack.auth.complete_phone_change(
            raw_access_token=login.access_token, new_phone=new_phone, grant=grant
        )
        assert session.user_id == user.user_id
        active = stack.store.connection.execute(
            """SELECT provider_subject FROM auth_identities
            WHERE user_id=? AND provider='phone' AND status='active'""",
            (user.user_id,),
        ).fetchone()
        assert active[0] == new_phone
        assert stack.reputations.find_by_phone(old_phone) is not None
        assert stack.reputations.find_by_phone(new_phone) is not None
    finally:
        stack.store.close()


def test_production_argon2_hashes_and_fast_hash_is_transparently_rehashed() -> None:
    fast = PasswordHashService(config=FAST_TEST_ARGON2_CONFIG)
    production = PasswordHashService(config=PRODUCTION_ARGON2_CONFIG)
    fast_hash = fast.hash_password(PASSWORD)
    verification = production.verify_password(fast_hash, PASSWORD)
    assert verification.valid
    assert verification.replacement_hash is not None
    assert production.verify_password(verification.replacement_hash, PASSWORD).valid


def test_password_policy_rejects_short_and_local_blocklisted_passwords() -> None:
    hasher = PasswordHashService(config=FAST_TEST_ARGON2_CONFIG)
    with pytest.raises(ContractError, match="15 and 128"):
        hasher.hash_password("too short")
    with pytest.raises(ContractError, match="blocked"):
        hasher.hash_password("passwordpassword")


def test_phone_block_is_explicit_and_prevents_existing_session_authentication(
    tmp_path: Path,
) -> None:
    stack = make_auth_stack(tmp_path)
    try:
        phone = "+8613810000108"
        session = stack.auth.complete_registration(
            phone=phone, grant=_registration_grant(stack, phone), password=PASSWORD
        )
        actor = SanctionActor("admin", "admin-1")
        blocked = stack.reputations.administrative_block_phone(
            phone,
            operation_id="op-block-1",
            actor=actor,
            reason_code="manual_abuse_review",
            now=stack.clock(),
        )
        assert blocked.status == "blocked"
        with pytest.raises(PhoneBlockedError):
            stack.auth.request_phone_code(
                phone,
                purpose=SmsPurpose.PHONE_CONTINUE,
                client_id="blocked-client",
                ip_address="203.0.113.7",
            )
        with pytest.raises(AuthenticationError):
            stack.auth.authenticate_access_token(session.access_token)
        repeated = stack.reputations.administrative_block_phone(
            phone,
            operation_id="op-block-2",
            actor=actor,
            reason_code="manual_abuse_review",
            now=stack.clock(),
        )
        assert repeated.status == "blocked"
        assert (
            stack.store.connection.execute(
                "SELECT COUNT(*) FROM phone_status_events WHERE event_type='phone_blocked'"
            ).fetchone()[0]
            == 1
        )
    finally:
        stack.store.close()


def test_final_registration_transaction_rechecks_phone_block_status(tmp_path: Path) -> None:
    stack = make_auth_stack(tmp_path)
    try:
        phone = "+8613810000111"
        user = make_test_account(stack, phone)
        stack.account.erase_account(user.user_id)
        grant = _registration_grant(stack, phone)
        stack.reputations.administrative_block_phone(
            phone,
            operation_id="block-between-grant-and-register",
            actor=SanctionActor("admin", "admin-race"),
            reason_code="manual_abuse_review",
            now=stack.clock(),
        )
        with pytest.raises(PhoneBlockedError):
            stack.auth.complete_registration(phone=phone, grant=grant, password=PASSWORD)
        assert stack.store.connection.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0
        assert (
            stack.store.connection.execute(
                "SELECT consumed_at FROM phone_verification_grants WHERE grant_id=?",
                (grant.grant_id,),
            ).fetchone()[0]
            is None
        )
    finally:
        stack.store.close()


def test_refresh_rotation_and_family_absolute_deadline(tmp_path: Path) -> None:
    clock = Clock()
    stack = make_auth_stack(tmp_path, clock=clock)
    try:
        phone = "+8613810000110"
        initial = stack.auth.complete_registration(
            phone=phone, grant=_registration_grant(stack, phone), password=PASSWORD
        )
        rotated = stack.auth.refresh_session(initial.refresh_token)
        assert rotated.session_family_id == initial.session_family_id
        with pytest.raises(AuthenticationError):
            stack.auth.refresh_session(initial.refresh_token)
        assert (
            stack.store.connection.execute(
                "SELECT revocation_reason FROM session_families WHERE session_family_id=?",
                (initial.session_family_id,),
            ).fetchone()[0]
            == "refresh_token_reuse"
        )

        fresh = stack.auth.login_with_password(
            phone=phone,
            password=PASSWORD,
            client_id="deadline-client",
            ip_address="203.0.113.8",
        )
        clock.tick(stack.auth.policy.family_absolute_ttl_ms)
        with pytest.raises(AuthenticationError):
            stack.auth.refresh_session(fresh.refresh_token)
        with pytest.raises(AuthenticationError):
            stack.auth.authenticate_access_token(fresh.access_token)
    finally:
        stack.store.close()
