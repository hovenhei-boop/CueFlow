from __future__ import annotations

import hmac
import json
import math
import sqlite3
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import cast

from cueflow.account import E164_PATTERN
from cueflow.account_store import AccountStore
from cueflow.auth_crypto import (
    HmacKeyring,
    PasswordHashService,
    bound_grant_hash,
    new_opaque_token,
    new_sms_code,
)
from cueflow.errors import (
    AccountStateError,
    AuthenticationError,
    AuthenticationRateLimitedError,
    ContractError,
    IdentityConflictError,
    SmsProviderUnavailableError,
)
from cueflow.phone_reputation import PhoneReputationService
from cueflow.sms import SmsProvider, SmsPurpose


def epoch_ms() -> int:
    return time.time_ns() // 1_000_000


@dataclass(frozen=True)
class AuthPolicy:
    access_ttl_ms: int = 15 * 60 * 1000
    refresh_ttl_ms: int = 30 * 24 * 60 * 60 * 1000
    family_absolute_ttl_ms: int = 180 * 24 * 60 * 60 * 1000
    sms_code_ttl_ms: int = 5 * 60 * 1000
    grant_ttl_ms: int = 10 * 60 * 1000
    security_event_ttl_ms: int = 30 * 24 * 60 * 60 * 1000
    rate_window_ms: int = 15 * 60 * 1000
    sms_phone_limit: int = 5
    sms_client_limit: int = 10
    sms_ip_limit: int = 20
    password_phone_limit: int = 5
    password_client_limit: int = 10
    password_ip_limit: int = 20


@dataclass(frozen=True)
class AuthSecrets:
    phone_keys: HmacKeyring = field(repr=False)
    grant_keys: HmacKeyring = field(repr=False)
    token_keys: HmacKeyring = field(repr=False)


@dataclass(frozen=True)
class SmsChallengeReceipt:
    challenge_id: str
    expires_at: int


@dataclass(frozen=True)
class VerificationGrant:
    grant_id: str
    raw_grant: str = field(repr=False)
    purpose: SmsPurpose
    expires_at: int


@dataclass(frozen=True)
class AuthenticatedSession:
    user_id: str
    session_family_id: str
    session_id: str
    access_token: str = field(repr=False)
    refresh_token: str = field(repr=False)
    csrf_token: str = field(repr=False)
    access_expires_at: int
    refresh_expires_at: int


class PhoneContinueKind(str, Enum):
    AUTHENTICATED = "authenticated"
    REGISTRATION_REQUIRED = "registration_required"


@dataclass(frozen=True)
class PhoneContinueResult:
    kind: PhoneContinueKind
    session: AuthenticatedSession | None = None
    registration_grant: VerificationGrant | None = field(default=None, repr=False)


class AuthService:
    def __init__(
        self,
        store: AccountStore,
        *,
        password_hasher: PasswordHashService,
        secrets: AuthSecrets,
        sms_provider: SmsProvider,
        phone_reputations: PhoneReputationService,
        clock: Callable[[], int] = epoch_ms,
        policy: AuthPolicy | None = None,
    ) -> None:
        self.store = store
        self.password_hasher = password_hasher
        self.secrets = secrets
        self.sms_provider = sms_provider
        self.phone_reputations = phone_reputations
        self._clock = clock
        self.policy = policy or AuthPolicy()

    def request_phone_code(
        self,
        phone: str,
        *,
        purpose: SmsPurpose,
        client_id: str,
        ip_address: str,
        user_id: str | None = None,
    ) -> SmsChallengeReceipt:
        _validate_phone(phone)
        now = self._clock()
        if purpose not in {SmsPurpose.ACCOUNT_ERASURE, SmsPurpose.PHONE_APPEAL}:
            self.phone_reputations.require_normal(phone)
        phone_key_id, phone_key = self._auth_key("phone", phone)
        client_key_id, client_key = self._auth_key("client", client_id)
        ip_key_id, ip_key = self._auth_key("ip", ip_address)
        limits = (
            ("phone", phone_key_id, phone_key, self.policy.sms_phone_limit),
            ("client", client_key_id, client_key, self.policy.sms_client_limit),
            ("ip", ip_key_id, ip_key, self.policy.sms_ip_limit),
        )
        challenge_id = "smc_" + uuid.uuid4().hex
        code = new_sms_code()
        code_hash = self._sms_code_hash(phone_key_id, challenge_id, phone_key, purpose, code)
        expires_at = now + self.policy.sms_code_ttl_ms
        retry_after = 0
        with self.store.transaction() as tx:
            if user_id is not None:
                self.store.user(user_id, tx)
            for bucket_type, key_id, bucket_key, limit in limits:
                retry_after = max(
                    retry_after,
                    self._consume_rate_limit_tx(
                        tx,
                        bucket_type=bucket_type,
                        key_id=key_id,
                        bucket_key=bucket_key,
                        purpose="sms:" + purpose.value,
                        limit=limit,
                        now=now,
                    ),
                )
            if retry_after:
                self._security_event_tx(
                    tx,
                    "sms_rate_limited",
                    now,
                    phone=(phone_key_id, phone_key),
                    client=(client_key_id, client_key),
                    ip=(ip_key_id, ip_key),
                )
            else:
                tx.execute(
                    """INSERT INTO sms_challenges VALUES (
                        ?, ?, ?, ?, ?, ?, 'pending', 5, ?, ?, NULL
                    )""",
                    (
                        challenge_id,
                        user_id,
                        phone_key_id,
                        phone_key,
                        purpose.value,
                        code_hash,
                        now,
                        expires_at,
                    ),
                )
        if retry_after:
            raise AuthenticationRateLimitedError(
                "authentication rate limit exceeded", retry_after_seconds=retry_after
            )
        try:
            self.sms_provider.send_code(phone=phone, code=code, purpose=purpose)
        except Exception as exc:
            with self.store.transaction() as tx:
                tx.execute(
                    "UPDATE sms_challenges SET status='failed' WHERE challenge_id=?",
                    (challenge_id,),
                )
            raise SmsProviderUnavailableError("SMS provider is temporarily unavailable") from exc
        with self.store.transaction() as tx:
            cursor = tx.execute(
                """UPDATE sms_challenges SET status='sent'
                WHERE challenge_id=? AND status='pending'""",
                (challenge_id,),
            )
            if cursor.rowcount != 1:
                raise AuthenticationError("SMS challenge could not be activated")
        return SmsChallengeReceipt(challenge_id, expires_at)

    def verify_phone_code(
        self,
        *,
        challenge_id: str,
        phone: str,
        code: str,
        purpose: SmsPurpose,
    ) -> PhoneContinueResult | VerificationGrant:
        _validate_phone(phone)
        now = self._clock()
        failed = False
        with self.store.transaction() as tx:
            row = tx.execute(
                "SELECT * FROM sms_challenges WHERE challenge_id=?", (challenge_id,)
            ).fetchone()
            if row is None:
                raise AuthenticationError("verification code is invalid or expired")
            phone_key_id, phone_key = self._auth_key(
                "phone", phone, key_id=str(row["phone_key_id"])
            )
            if now >= int(row["expires_at"]):
                tx.execute(
                    "UPDATE sms_challenges SET status='expired' WHERE challenge_id=?",
                    (challenge_id,),
                )
                raise AuthenticationError("verification code is invalid or expired")
            bound = (
                row["status"] == "sent"
                and row["purpose"] == purpose.value
                and row["phone_key_id"] == phone_key_id
                and hmac.compare_digest(bytes(row["phone_key"]), phone_key)
            )
            expected = self._sms_code_hash(phone_key_id, challenge_id, phone_key, purpose, code)
            if not bound or not hmac.compare_digest(bytes(row["code_hash"]), expected):
                attempts = max(0, int(row["attempts_remaining"]) - 1)
                tx.execute(
                    """UPDATE sms_challenges SET attempts_remaining=?, status=?
                    WHERE challenge_id=?""",
                    (attempts, "failed" if attempts == 0 else "sent", challenge_id),
                )
                self._security_event_tx(
                    tx, "sms_code_failure", now, phone=(phone_key_id, phone_key)
                )
                failed = True
            else:
                if purpose not in {SmsPurpose.ACCOUNT_ERASURE, SmsPurpose.PHONE_APPEAL}:
                    self.phone_reputations.require_normal(phone, connection=tx)
                tx.execute(
                    """UPDATE sms_challenges SET status='verified', verified_at=?
                    WHERE challenge_id=?""",
                    (now, challenge_id),
                )
                identity = tx.execute(
                    """SELECT * FROM auth_identities
                    WHERE provider='phone' AND provider_subject=? AND status='active'""",
                    (phone,),
                ).fetchone()
                if purpose is SmsPurpose.PHONE_CONTINUE and identity is not None:
                    user_id = str(identity["user_id"])
                    self._require_effective_user_tx(tx, user_id, phone)
                    session = self._create_login_state_tx(tx, user_id, now)
                    return PhoneContinueResult(PhoneContinueKind.AUTHENTICATED, session=session)
                grant = self._create_grant_tx(
                    tx,
                    challenge_id=challenge_id,
                    user_id=None if row["user_id"] is None else str(row["user_id"]),
                    phone_key_id=phone_key_id,
                    phone_key=phone_key,
                    purpose=purpose,
                    now=now,
                )
                if purpose is SmsPurpose.PHONE_CONTINUE:
                    return PhoneContinueResult(
                        PhoneContinueKind.REGISTRATION_REQUIRED,
                        registration_grant=grant,
                    )
                return grant
        if failed:
            raise AuthenticationError("verification code is invalid or expired")
        raise AssertionError("SMS verification did not produce a result")

    def complete_registration(
        self, *, phone: str, grant: VerificationGrant, password: str
    ) -> AuthenticatedSession:
        _validate_phone(phone)
        if grant.purpose is not SmsPurpose.PHONE_CONTINUE:
            raise AuthenticationError("registration grant is invalid or expired")
        password_hash = self.password_hasher.hash_password(password)
        now = self._clock()
        user_id = "usr_" + uuid.uuid4().hex
        identity_id = "idn_" + uuid.uuid4().hex
        try:
            with self.store.transaction() as tx:
                self._verify_grant_tx(tx, grant, phone, now)
                if tx.execute(
                    """SELECT 1 FROM auth_identities
                    WHERE provider='phone' AND provider_subject=? AND status='active'""",
                    (phone,),
                ).fetchone():
                    raise IdentityConflictError("phone is already registered")
                self.phone_reputations.require_normal(phone, connection=tx)
                tx.execute("INSERT INTO users VALUES (?, 'active', ?, ?)", (user_id, now, now))
                reputation = self.phone_reputations.ensure_phone_tx(tx, phone, now=now)
                tx.execute(
                    """INSERT INTO auth_identities VALUES (
                        ?, ?, 'phone', ?, 'active', 'sms', ?, ?, NULL, NULL, ?
                    )""",
                    (
                        identity_id,
                        user_id,
                        phone,
                        now,
                        now,
                        reputation.phone_reputation_id,
                    ),
                )
                tx.execute(
                    "INSERT INTO password_credentials VALUES (?, ?, ?, ?)",
                    (user_id, password_hash, now, now),
                )
                session = self._create_login_state_tx(tx, user_id, now)
                self._consume_grant_tx(tx, grant.grant_id, now)
                _audit(tx, user_id, "user_registered", identity_id, None, now)
                return session
        except sqlite3.IntegrityError as exc:
            raise IdentityConflictError("registration conflicts with persisted state") from exc

    def cancel_registration(self, grant: VerificationGrant) -> None:
        if grant.purpose is not SmsPurpose.PHONE_CONTINUE:
            raise ContractError("only a registration grant can be cancelled")
        with self.store.transaction() as tx:
            row = tx.execute(
                "SELECT challenge_id, consumed_at FROM phone_verification_grants WHERE grant_id=?",
                (grant.grant_id,),
            ).fetchone()
            if row is None or row["consumed_at"] is not None:
                return
            tx.execute("DELETE FROM phone_verification_grants WHERE grant_id=?", (grant.grant_id,))
            tx.execute("DELETE FROM sms_challenges WHERE challenge_id=?", (row["challenge_id"],))

    def login_with_password(
        self, *, phone: str, password: str, client_id: str, ip_address: str
    ) -> AuthenticatedSession:
        _validate_phone(phone)
        now = self._clock()
        self.phone_reputations.require_normal(phone)
        phone_key_id, phone_key = self._auth_key("phone", phone)
        client_key_id, client_key = self._auth_key("client", client_id)
        ip_key_id, ip_key = self._auth_key("ip", ip_address)
        failed = False
        with self.store.transaction() as tx:
            for bucket_type, key_id, bucket_key, limit in (
                ("phone", phone_key_id, phone_key, self.policy.password_phone_limit),
                ("client", client_key_id, client_key, self.policy.password_client_limit),
                ("ip", ip_key_id, ip_key, self.policy.password_ip_limit),
            ):
                self._check_rate_limit_tx(
                    tx,
                    bucket_type=bucket_type,
                    key_id=key_id,
                    bucket_key=bucket_key,
                    purpose="password_login",
                    limit=limit,
                    now=now,
                )
            row = tx.execute(
                """SELECT i.user_id, u.status, p.password_hash
                FROM auth_identities i
                JOIN users u ON u.user_id=i.user_id
                JOIN password_credentials p ON p.user_id=i.user_id
                WHERE i.provider='phone' AND i.provider_subject=? AND i.status='active'""",
                (phone,),
            ).fetchone()
            verification = self.password_hasher.verify_password_or_dummy(
                None if row is None else str(row["password_hash"]), password
            )
            if row is None or not verification.valid or row["status"] != "active":
                for bucket_type, key_id, bucket_key, limit in (
                    ("phone", phone_key_id, phone_key, self.policy.password_phone_limit),
                    ("client", client_key_id, client_key, self.policy.password_client_limit),
                    ("ip", ip_key_id, ip_key, self.policy.password_ip_limit),
                ):
                    self._consume_rate_limit_tx(
                        tx,
                        bucket_type=bucket_type,
                        key_id=key_id,
                        bucket_key=bucket_key,
                        purpose="password_login",
                        limit=limit,
                        now=now,
                    )
                self._security_event_tx(
                    tx,
                    "password_failure",
                    now,
                    phone=(phone_key_id, phone_key),
                    client=(client_key_id, client_key),
                    ip=(ip_key_id, ip_key),
                )
                failed = True
            else:
                user_id = str(row["user_id"])
                self._require_effective_user_tx(tx, user_id, phone)
                if verification.replacement_hash is not None:
                    tx.execute(
                        """UPDATE password_credentials
                        SET password_hash=?, updated_at=? WHERE user_id=?""",
                        (verification.replacement_hash, now, user_id),
                    )
                self._clear_password_rate_limits_tx(
                    tx,
                    (
                        ("phone", phone_key_id, phone_key),
                        ("client", client_key_id, client_key),
                        ("ip", ip_key_id, ip_key),
                    ),
                )
                return self._create_login_state_tx(tx, user_id, now)
        if failed:
            raise AuthenticationError("phone or password is incorrect")
        raise AssertionError("password login did not produce a result")

    def refresh_session(self, raw_refresh_token: str) -> AuthenticatedSession:
        now = self._clock()
        digest = self._token_digest("refresh-token", raw_refresh_token)
        reuse_detected = False
        with self.store.transaction() as tx:
            old = tx.execute(
                """SELECT s.*, f.created_at AS family_created_at, f.revoked_at AS family_revoked_at,
                    u.status AS user_status
                FROM sessions s
                JOIN session_families f ON f.session_family_id=s.session_family_id
                JOIN users u ON u.user_id=s.user_id
                WHERE s.refresh_token_hash=?""",
                (digest,),
            ).fetchone()
            if old is None:
                raise AuthenticationError("refresh token is invalid")
            if not self._session_row_active(old, now):
                if old["replaced_by_session_id"] is not None:
                    self._revoke_family_tx(
                        tx,
                        str(old["session_family_id"]),
                        str(old["user_id"]),
                        now,
                        "refresh_token_reuse",
                    )
                    self._security_event_tx(tx, "token_reuse", now)
                    reuse_detected = True
                else:
                    raise AuthenticationError("refresh token is invalid")
            else:
                phone_row = tx.execute(
                    """SELECT provider_subject FROM auth_identities
                    WHERE user_id=? AND provider='phone' AND status='active'""",
                    (old["user_id"],),
                ).fetchone()
                if phone_row is None:
                    raise AccountStateError("User does not have exactly one active phone")
                self.phone_reputations.require_normal(
                    str(phone_row["provider_subject"]), connection=tx
                )
                session = self._rotate_login_state_tx(tx, old, now)
                return session
        if reuse_detected:
            raise AuthenticationError("refresh token is invalid")
        raise AssertionError("refresh did not produce a result")

    def authenticate_access_token(self, raw_access_token: str) -> str:
        return str(self._access_context(raw_access_token)["user_id"])

    def _access_context(
        self,
        raw_access_token: str,
        *,
        connection: sqlite3.Connection | None = None,
    ) -> sqlite3.Row:
        now = self._clock()
        digest = self._token_digest("access-token", raw_access_token)
        tx = connection or self.store.connection
        row = tx.execute(
            """SELECT a.*, s.revoked_at AS session_revoked_at,
                s.replaced_by_session_id AS session_replaced_by_session_id,
                s.expires_at AS session_expires_at,
                f.created_at AS family_created_at, f.revoked_at AS family_revoked_at,
                u.status AS user_status, i.provider_subject
            FROM access_tokens a
            JOIN sessions s ON s.session_id=a.session_id
            JOIN session_families f ON f.session_family_id=s.session_family_id
            JOIN users u ON u.user_id=a.user_id
            JOIN auth_identities i ON i.user_id=a.user_id
                AND i.provider='phone' AND i.status='active'
            WHERE a.token_hash=?""",
            (digest,),
        ).fetchone()
        if (
            row is None
            or row["revoked_at"] is not None
            or now >= int(row["expires_at"])
            or not self._session_row_active(row, now, prefix="session_")
        ):
            raise AuthenticationError("access token is invalid")
        self.phone_reputations.require_normal(str(row["provider_subject"]), connection=tx)
        return cast(sqlite3.Row, row)

    def change_password(
        self, *, raw_access_token: str, current_password: str, new_password: str
    ) -> AuthenticatedSession:
        new_hash = self.password_hasher.hash_password(new_password)
        now = self._clock()
        with self.store.transaction() as tx:
            user_id = str(self._access_context(raw_access_token, connection=tx)["user_id"])
            row = tx.execute(
                "SELECT password_hash FROM password_credentials WHERE user_id=?", (user_id,)
            ).fetchone()
            if (
                row is None
                or not self.password_hasher.verify_password(
                    str(row["password_hash"]), current_password
                ).valid
            ):
                raise AuthenticationError("current password is incorrect")
            phone = self._active_phone_tx(tx, user_id)
            self._require_effective_user_tx(tx, user_id, phone)
            tx.execute(
                "UPDATE password_credentials SET password_hash=?, updated_at=? WHERE user_id=?",
                (new_hash, now, user_id),
            )
            self._revoke_all_tx(tx, user_id, now, "password_changed")
            _audit(tx, user_id, "password_changed", None, "password_changed", now)
            return self._create_login_state_tx(tx, user_id, now)

    def reset_password(self, *, phone: str, grant: VerificationGrant, new_password: str) -> None:
        _validate_phone(phone)
        if grant.purpose is not SmsPurpose.PASSWORD_RESET:
            raise AuthenticationError("password reset grant is invalid or expired")
        new_hash = self.password_hasher.hash_password(new_password)
        now = self._clock()
        with self.store.transaction() as tx:
            self._verify_grant_tx(tx, grant, phone, now)
            identity = tx.execute(
                """SELECT user_id FROM auth_identities
                WHERE provider='phone' AND provider_subject=? AND status='active'""",
                (phone,),
            ).fetchone()
            if identity is None:
                raise AuthenticationError("password reset grant is invalid or expired")
            user_id = str(identity["user_id"])
            self._require_effective_user_tx(tx, user_id, phone)
            tx.execute(
                "UPDATE password_credentials SET password_hash=?, updated_at=? WHERE user_id=?",
                (new_hash, now, user_id),
            )
            self._revoke_all_tx(tx, user_id, now, "password_reset")
            self._consume_grant_tx(tx, grant.grant_id, now)
            _audit(tx, user_id, "password_reset", None, "password_reset", now)

    def request_phone_change_code(
        self,
        *,
        raw_access_token: str,
        current_password: str,
        new_phone: str,
        client_id: str,
        ip_address: str,
    ) -> SmsChallengeReceipt:
        _validate_phone(new_phone)
        user_id = str(self._access_context(raw_access_token)["user_id"])
        row = self.store.connection.execute(
            "SELECT password_hash FROM password_credentials WHERE user_id=?", (user_id,)
        ).fetchone()
        if (
            row is None
            or not self.password_hasher.verify_password(
                str(row["password_hash"]), current_password
            ).valid
        ):
            raise AuthenticationError("current password is incorrect")
        self.phone_reputations.require_normal(new_phone)
        existing = self.store.connection.execute(
            """SELECT user_id FROM auth_identities
            WHERE provider='phone' AND provider_subject=? AND status='active'""",
            (new_phone,),
        ).fetchone()
        if existing is not None:
            raise IdentityConflictError("phone_already_registered")
        return self.request_phone_code(
            new_phone,
            purpose=SmsPurpose.PHONE_CHANGE,
            client_id=client_id,
            ip_address=ip_address,
            user_id=user_id,
        )

    def complete_phone_change(
        self, *, raw_access_token: str, new_phone: str, grant: VerificationGrant
    ) -> AuthenticatedSession:
        _validate_phone(new_phone)
        if grant.purpose is not SmsPurpose.PHONE_CHANGE:
            raise AuthenticationError("phone change grant is invalid or expired")
        now = self._clock()
        with self.store.transaction() as tx:
            user_id = str(self._access_context(raw_access_token, connection=tx)["user_id"])
            self._verify_grant_tx(tx, grant, new_phone, now, user_id=user_id)
            self._require_effective_user_tx(tx, user_id, self._active_phone_tx(tx, user_id))
            if tx.execute(
                """SELECT 1 FROM auth_identities
                WHERE provider='phone' AND provider_subject=? AND status='active'""",
                (new_phone,),
            ).fetchone():
                raise IdentityConflictError("phone_already_registered")
            self.phone_reputations.require_normal(new_phone, connection=tx)
            reputation = self.phone_reputations.ensure_phone_tx(tx, new_phone, now=now)
            current = tx.execute(
                """SELECT identity_id FROM auth_identities
                WHERE user_id=? AND provider='phone' AND status='active'""",
                (user_id,),
            ).fetchone()
            assert current is not None
            new_identity_id = "idn_" + uuid.uuid4().hex
            tx.execute(
                """UPDATE auth_identities SET status='detached', detached_at=?,
                    replaced_by_identity_id=? WHERE identity_id=?""",
                (now, new_identity_id, current["identity_id"]),
            )
            tx.execute(
                """INSERT INTO auth_identities VALUES (
                    ?, ?, 'phone', ?, 'active', 'sms', ?, ?, NULL, NULL, ?
                )""",
                (
                    new_identity_id,
                    user_id,
                    new_phone,
                    now,
                    now,
                    reputation.phone_reputation_id,
                ),
            )
            tx.execute("UPDATE users SET updated_at=? WHERE user_id=?", (now, user_id))
            self._revoke_all_tx(tx, user_id, now, "phone_changed")
            self._consume_grant_tx(tx, grant.grant_id, now)
            _audit(tx, user_id, "phone_changed", new_identity_id, "phone_changed", now)
            return self._create_login_state_tx(tx, user_id, now)

    def logout_current_session(self, raw_access_token: str) -> None:
        now = self._clock()
        with self.store.transaction() as tx:
            context = self._access_context(raw_access_token, connection=tx)
            session_id = str(context["session_id"])
            row = tx.execute(
                "SELECT user_id FROM sessions WHERE session_id=?", (session_id,)
            ).fetchone()
            if row is None:
                return
            tx.execute(
                "UPDATE sessions SET revoked_at=? WHERE session_id=? AND revoked_at IS NULL",
                (now, session_id),
            )
            tx.execute(
                "UPDATE access_tokens SET revoked_at=? WHERE session_id=? AND revoked_at IS NULL",
                (now, session_id),
            )
            _audit(tx, str(row["user_id"]), "session_revoked", session_id, "user_request", now)

    def logout_all_sessions(self, raw_access_token: str) -> None:
        now = self._clock()
        with self.store.transaction() as tx:
            user_id = str(self._access_context(raw_access_token, connection=tx)["user_id"])
            self.store.user(user_id, tx)
            self._revoke_all_tx(tx, user_id, now, "logout_all")
            _audit(tx, user_id, "all_sessions_revoked", None, "logout_all", now)

    def request_account_erasure_code(
        self,
        *,
        phone: str,
        client_id: str,
        ip_address: str,
    ) -> SmsChallengeReceipt:
        _validate_phone(phone)
        row = self.store.connection.execute(
            """SELECT u.user_id FROM users u
            JOIN auth_identities i ON i.user_id=u.user_id
                AND i.provider='phone' AND i.status='active'
            JOIN account_qualifying_bans b ON b.user_id=u.user_id
                AND b.overturned_at IS NULL
            WHERE i.provider_subject=? AND u.status='suspended'""",
            (phone,),
        ).fetchone()
        if row is None:
            raise AuthenticationError("account is not eligible for dedicated erasure")
        return self.request_phone_code(
            phone,
            purpose=SmsPurpose.ACCOUNT_ERASURE,
            client_id=client_id,
            ip_address=ip_address,
            user_id=str(row["user_id"]),
        )

    def erase_qualifying_banned_account(self, *, phone: str, grant: VerificationGrant) -> None:
        if grant.purpose is not SmsPurpose.ACCOUNT_ERASURE:
            raise AuthenticationError("account erasure grant is invalid or expired")
        now = self._clock()
        with self.store.transaction() as tx:
            grant_row = self._verify_grant_tx(tx, grant, phone, now)
            if grant_row["user_id"] is None:
                raise AuthenticationError("account erasure grant is invalid or expired")
            user_id = str(grant_row["user_id"])
            user = self.store.user(user_id, tx)
            if (
                user["status"] != "suspended"
                or not tx.execute(
                    """SELECT 1 FROM account_qualifying_bans
                    WHERE user_id=? AND overturned_at IS NULL""",
                    (user_id,),
                ).fetchone()
            ):
                raise AccountStateError(
                    "dedicated erasure requires a suspended qualifying-ban account"
                )
            if self._active_phone_tx(tx, user_id) != phone:
                raise AuthenticationError("account erasure grant is invalid or expired")
            self._consume_grant_tx(tx, grant.grant_id, now)
            for table in (
                "access_tokens",
                "account_audit_events",
                "account_qualifying_bans",
                "sessions",
                "session_families",
                "password_credentials",
                "auth_identities",
                "users",
            ):
                tx.execute(f"DELETE FROM {table} WHERE user_id=?", (user_id,))

    def purge_expired_auth_state(self) -> tuple[int, int, int]:
        now = self._clock()
        with self.store.transaction() as tx:
            grants = tx.execute(
                "DELETE FROM phone_verification_grants WHERE expires_at<=?", (now,)
            ).rowcount
            challenges = tx.execute(
                "DELETE FROM sms_challenges WHERE expires_at<=?", (now,)
            ).rowcount
            events = tx.execute(
                "DELETE FROM auth_security_events WHERE expires_at<=?", (now,)
            ).rowcount
        return int(challenges), int(grants), int(events)

    def _create_grant_tx(
        self,
        tx: sqlite3.Connection,
        *,
        challenge_id: str,
        user_id: str | None,
        phone_key_id: str,
        phone_key: bytes,
        purpose: SmsPurpose,
        now: int,
    ) -> VerificationGrant:
        grant_id = "grt_" + uuid.uuid4().hex
        raw_grant = new_opaque_token()
        expires_at = now + self.policy.grant_ttl_ms
        grant_key_id, grant_hash = bound_grant_hash(
            self.secrets.grant_keys,
            raw_grant=raw_grant,
            grant_id=grant_id,
            phone_key=phone_key,
            purpose=purpose.value,
            expires_at=expires_at,
        )
        tx.execute(
            """INSERT INTO phone_verification_grants VALUES (
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL
            )""",
            (
                grant_id,
                challenge_id,
                user_id,
                phone_key_id,
                phone_key,
                purpose.value,
                grant_key_id,
                grant_hash,
                now,
                expires_at,
            ),
        )
        return VerificationGrant(grant_id, raw_grant, purpose, expires_at)

    def _verify_grant_tx(
        self,
        tx: sqlite3.Connection,
        grant: VerificationGrant,
        phone: str,
        now: int,
        *,
        user_id: str | None = None,
    ) -> sqlite3.Row:
        row = tx.execute(
            "SELECT * FROM phone_verification_grants WHERE grant_id=?", (grant.grant_id,)
        ).fetchone()
        if row is None:
            raise AuthenticationError("verification grant is invalid or expired")
        phone_key_id, phone_key = self._auth_key("phone", phone, key_id=str(row["phone_key_id"]))
        key_id, expected = bound_grant_hash(
            self.secrets.grant_keys,
            raw_grant=grant.raw_grant,
            grant_id=grant.grant_id,
            phone_key=phone_key,
            purpose=grant.purpose.value,
            expires_at=int(row["expires_at"]),
            key_id=str(row["grant_key_id"]),
        )
        valid = (
            row["consumed_at"] is None
            and now < int(row["expires_at"])
            and row["purpose"] == grant.purpose.value
            and int(row["expires_at"]) == grant.expires_at
            and row["phone_key_id"] == phone_key_id
            and row["grant_key_id"] == key_id
            and hmac.compare_digest(bytes(row["phone_key"]), phone_key)
            and hmac.compare_digest(bytes(row["grant_hash"]), expected)
            and (user_id is None or row["user_id"] == user_id)
        )
        if not valid:
            raise AuthenticationError("verification grant is invalid or expired")
        return cast(sqlite3.Row, row)

    @staticmethod
    def _consume_grant_tx(tx: sqlite3.Connection, grant_id: str, now: int) -> None:
        cursor = tx.execute(
            """UPDATE phone_verification_grants SET consumed_at=?
            WHERE grant_id=? AND consumed_at IS NULL""",
            (now, grant_id),
        )
        if cursor.rowcount != 1:
            raise AuthenticationError("verification grant is invalid or expired")

    def _create_login_state_tx(
        self, tx: sqlite3.Connection, user_id: str, now: int
    ) -> AuthenticatedSession:
        self._evict_excess_families_tx(tx, user_id, now)
        family_id = "sfm_" + uuid.uuid4().hex
        tx.execute(
            "INSERT INTO session_families VALUES (?, ?, ?, NULL, NULL)",
            (family_id, user_id, now),
        )
        return self._insert_session_and_access_tx(tx, user_id, family_id, now)

    def _rotate_login_state_tx(
        self, tx: sqlite3.Connection, old: sqlite3.Row, now: int
    ) -> AuthenticatedSession:
        tx.execute("UPDATE sessions SET revoked_at=? WHERE session_id=?", (now, old["session_id"]))
        tx.execute(
            "UPDATE access_tokens SET revoked_at=? WHERE session_id=? AND revoked_at IS NULL",
            (now, old["session_id"]),
        )
        result = self._insert_session_and_access_tx(
            tx, str(old["user_id"]), str(old["session_family_id"]), now
        )
        tx.execute(
            "UPDATE sessions SET replaced_by_session_id=? WHERE session_id=?",
            (result.session_id, old["session_id"]),
        )
        _audit(tx, result.user_id, "session_rotated", result.session_id, None, now)
        return result

    def _insert_session_and_access_tx(
        self, tx: sqlite3.Connection, user_id: str, family_id: str, now: int
    ) -> AuthenticatedSession:
        session_id = "ses_" + uuid.uuid4().hex
        access_token_id = "atk_" + uuid.uuid4().hex
        raw_refresh = self._new_keyed_token()
        raw_access = self._new_keyed_token()
        raw_csrf = self._new_keyed_token()
        refresh_expires = now + self.policy.refresh_ttl_ms
        access_expires = now + self.policy.access_ttl_ms
        tx.execute(
            "INSERT INTO sessions VALUES (?, ?, ?, ?, ?, ?, NULL, NULL)",
            (
                session_id,
                user_id,
                family_id,
                self._token_digest("refresh-token", raw_refresh),
                now,
                refresh_expires,
            ),
        )
        tx.execute(
            "INSERT INTO access_tokens VALUES (?, ?, ?, ?, ?, ?, ?, NULL)",
            (
                access_token_id,
                user_id,
                session_id,
                self._token_digest("access-token", raw_access),
                self._token_digest("csrf-token", raw_csrf),
                now,
                access_expires,
            ),
        )
        _audit(tx, user_id, "session_created", session_id, None, now)
        return AuthenticatedSession(
            user_id,
            family_id,
            session_id,
            raw_access,
            raw_refresh,
            raw_csrf,
            access_expires,
            refresh_expires,
        )

    def _evict_excess_families_tx(self, tx: sqlite3.Connection, user_id: str, now: int) -> None:
        rows = tx.execute(
            """SELECT f.session_family_id FROM session_families f
            WHERE f.user_id=? AND f.revoked_at IS NULL
              AND f.created_at+?>?
              AND EXISTS (
                  SELECT 1 FROM sessions s WHERE s.session_family_id=f.session_family_id
                  AND s.revoked_at IS NULL AND s.replaced_by_session_id IS NULL
                  AND s.expires_at>?
              )
            ORDER BY f.created_at, f.session_family_id""",
            (user_id, self.policy.family_absolute_ttl_ms, now, now),
        ).fetchall()
        while len(rows) >= 5:
            family_id = str(rows.pop(0)["session_family_id"])
            self._revoke_family_tx(tx, family_id, user_id, now, "session_limit_eviction")
            _audit(tx, user_id, "session_family_evicted", family_id, None, now)

    def _session_row_active(self, row: sqlite3.Row, now: int, *, prefix: str = "") -> bool:
        return (
            row[prefix + "revoked_at"] is None
            and row[prefix + "replaced_by_session_id"] is None
            and now < int(row[prefix + "expires_at"])
            and row["family_revoked_at"] is None
            and now < int(row["family_created_at"]) + self.policy.family_absolute_ttl_ms
            and row["user_status"] == "active"
        )

    def _require_effective_user_tx(self, tx: sqlite3.Connection, user_id: str, phone: str) -> None:
        row = self.store.user(user_id, tx)
        if row["status"] != "active":
            raise AccountStateError("suspended User cannot authenticate")
        if (
            tx.execute("SELECT 1 FROM password_credentials WHERE user_id=?", (user_id,)).fetchone()
            is None
        ):
            raise AccountStateError("User does not have a password credential")
        self.phone_reputations.require_normal(phone, connection=tx)

    @staticmethod
    def _active_phone_tx(tx: sqlite3.Connection, user_id: str) -> str:
        row = tx.execute(
            """SELECT provider_subject FROM auth_identities
            WHERE user_id=? AND provider='phone' AND status='active'""",
            (user_id,),
        ).fetchone()
        if row is None:
            raise AccountStateError("User does not have exactly one active phone")
        return str(row["provider_subject"])

    def _auth_key(
        self, purpose: str, value: str, *, key_id: str | None = None
    ) -> tuple[str, bytes]:
        key = (
            self.secrets.phone_keys.active
            if key_id is None
            else self.secrets.phone_keys.key(key_id)
        )
        return key.key_id, self.secrets.phone_keys.digest(
            "auth-" + purpose + "-v1", value.encode("utf-8"), key_id=key.key_id
        )

    def _sms_code_hash(
        self,
        key_id: str,
        challenge_id: str,
        phone_key: bytes,
        purpose: SmsPurpose,
        code: str,
    ) -> bytes:
        payload = (
            challenge_id.encode("ascii")
            + phone_key
            + purpose.value.encode("ascii")
            + code.encode("ascii")
        )
        return self.secrets.phone_keys.digest("sms-code-v1", payload, key_id=key_id)

    def _new_keyed_token(self) -> str:
        return self.secrets.token_keys.active.key_id + "." + new_opaque_token()

    def _token_digest(self, purpose: str, raw_token: str) -> str:
        try:
            key_id, _ = raw_token.split(".", 1)
        except ValueError as exc:
            raise AuthenticationError("token is invalid") from exc
        try:
            return self.secrets.token_keys.token_digest(purpose, raw_token, key_id=key_id)
        except ContractError as exc:
            raise AuthenticationError("token is invalid") from exc

    def verify_csrf_token(self, raw_access_token: str, raw_csrf_token: str) -> bool:
        try:
            access_digest = self._token_digest("access-token", raw_access_token)
            csrf_digest = self._token_digest("csrf-token", raw_csrf_token)
        except AuthenticationError:
            return False
        row = self.store.connection.execute(
            "SELECT csrf_token_hash FROM access_tokens WHERE token_hash=?", (access_digest,)
        ).fetchone()
        return row is not None and hmac.compare_digest(str(row["csrf_token_hash"]), csrf_digest)

    def _check_rate_limit_tx(
        self,
        tx: sqlite3.Connection,
        *,
        bucket_type: str,
        key_id: str,
        bucket_key: bytes,
        purpose: str,
        limit: int,
        now: int,
    ) -> None:
        row = tx.execute(
            """SELECT * FROM auth_rate_limit_buckets
            WHERE bucket_type=? AND key_id=? AND bucket_key=? AND purpose=?""",
            (bucket_type, key_id, bucket_key, purpose),
        ).fetchone()
        if row is None:
            return
        if now < int(row["cooldown_until"]):
            retry = max(1, math.ceil((int(row["cooldown_until"]) - now) / 1000))
            raise AuthenticationRateLimitedError(
                "authentication rate limit exceeded", retry_after_seconds=retry
            )
        if (
            now - int(row["window_started_at"]) < self.policy.rate_window_ms
            and int(row["attempt_count"]) >= limit
        ):
            raise AuthenticationRateLimitedError(
                "authentication rate limit exceeded", retry_after_seconds=30
            )

    def _consume_rate_limit_tx(
        self,
        tx: sqlite3.Connection,
        *,
        bucket_type: str,
        key_id: str,
        bucket_key: bytes,
        purpose: str,
        limit: int,
        now: int,
    ) -> int:
        row = tx.execute(
            """SELECT * FROM auth_rate_limit_buckets
            WHERE bucket_type=? AND key_id=? AND bucket_key=? AND purpose=?""",
            (bucket_type, key_id, bucket_key, purpose),
        ).fetchone()
        if row is None or now - int(row["window_started_at"]) >= self.policy.rate_window_ms:
            tx.execute(
                """INSERT INTO auth_rate_limit_buckets VALUES (?, ?, ?, ?, ?, 1, 0, ?)
                ON CONFLICT(bucket_type, key_id, bucket_key, purpose) DO UPDATE SET
                    window_started_at=excluded.window_started_at,
                    attempt_count=1, cooldown_until=0, updated_at=excluded.updated_at""",
                (bucket_type, key_id, bucket_key, purpose, now, now),
            )
            return 0
        if now < int(row["cooldown_until"]):
            retry = max(1, math.ceil((int(row["cooldown_until"]) - now) / 1000))
            return retry
        attempts = int(row["attempt_count"]) + 1
        cooldown_until = 0
        if attempts > limit:
            cooldown_seconds = min(30 * (2 ** (attempts - limit - 1)), 15 * 60)
            cooldown_until = now + cooldown_seconds * 1000
        tx.execute(
            """UPDATE auth_rate_limit_buckets
            SET attempt_count=?, cooldown_until=?, updated_at=?
            WHERE bucket_type=? AND key_id=? AND bucket_key=? AND purpose=?""",
            (
                attempts,
                cooldown_until,
                now,
                bucket_type,
                key_id,
                bucket_key,
                purpose,
            ),
        )
        if cooldown_until:
            return max(1, math.ceil((cooldown_until - now) / 1000))
        return 0

    @staticmethod
    def _clear_password_rate_limits_tx(
        tx: sqlite3.Connection, values: tuple[tuple[str, str, bytes], ...]
    ) -> None:
        for bucket_type, key_id, bucket_key in values:
            tx.execute(
                """DELETE FROM auth_rate_limit_buckets
                WHERE bucket_type=? AND key_id=? AND bucket_key=? AND purpose='password_login'""",
                (bucket_type, key_id, bucket_key),
            )

    def _security_event_tx(
        self,
        tx: sqlite3.Connection,
        event_type: str,
        now: int,
        *,
        phone: tuple[str, bytes] | None = None,
        client: tuple[str, bytes] | None = None,
        ip: tuple[str, bytes] | None = None,
    ) -> None:
        tx.execute(
            "INSERT INTO auth_security_events VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "ase_" + uuid.uuid4().hex,
                event_type,
                None if phone is None else phone[0],
                None if phone is None else phone[1],
                None if client is None else client[0],
                None if client is None else client[1],
                None if ip is None else ip[0],
                None if ip is None else ip[1],
                now,
                now + self.policy.security_event_ttl_ms,
            ),
        )

    @staticmethod
    def _revoke_family_tx(
        tx: sqlite3.Connection,
        family_id: str,
        user_id: str,
        now: int,
        reason: str,
    ) -> None:
        tx.execute(
            """UPDATE session_families SET revoked_at=?, revocation_reason=?
            WHERE session_family_id=? AND revoked_at IS NULL""",
            (now, reason, family_id),
        )
        tx.execute(
            "UPDATE sessions SET revoked_at=? WHERE session_family_id=? AND revoked_at IS NULL",
            (now, family_id),
        )
        tx.execute(
            """UPDATE access_tokens SET revoked_at=?
            WHERE user_id=? AND session_id IN (
                SELECT session_id FROM sessions WHERE session_family_id=?
            ) AND revoked_at IS NULL""",
            (now, user_id, family_id),
        )

    def _revoke_all_tx(self, tx: sqlite3.Connection, user_id: str, now: int, reason: str) -> None:
        tx.execute(
            """UPDATE session_families SET revoked_at=?, revocation_reason=?
            WHERE user_id=? AND revoked_at IS NULL""",
            (now, reason, user_id),
        )
        tx.execute(
            "UPDATE sessions SET revoked_at=? WHERE user_id=? AND revoked_at IS NULL",
            (now, user_id),
        )
        tx.execute(
            "UPDATE access_tokens SET revoked_at=? WHERE user_id=? AND revoked_at IS NULL",
            (now, user_id),
        )


def _validate_phone(phone: str) -> None:
    if not E164_PATTERN.fullmatch(phone):
        raise ContractError("phone must be canonical E.164")


def _audit(
    tx: sqlite3.Connection,
    user_id: str,
    event_type: str,
    subject_id: str | None,
    reason: str | None,
    now: int,
) -> None:
    metadata = json.dumps(
        {} if reason is None else {"reason": reason},
        sort_keys=True,
        separators=(",", ":"),
    )
    tx.execute(
        "INSERT INTO account_audit_events VALUES (?, ?, ?, ?, ?, ?)",
        ("evt_" + uuid.uuid4().hex, user_id, event_type, subject_id, metadata, now),
    )
