from __future__ import annotations

import json
import re
import sqlite3
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, cast

from cueflow.account_migrations import (
    ACCOUNT_OWNED_TABLES,
    SESSION_REVOCATION_REASONS,
)
from cueflow.account_store import AccountStore
from cueflow.errors import (
    AccountStateError,
    ContractError,
    IdentityConflictError,
    SessionStateError,
)

MAX_ACTIVE_SESSION_FAMILIES = 5
SESSION_FAMILY_ABSOLUTE_TTL_MS = 180 * 24 * 60 * 60 * 1000
ACCOUNT_ERASURE_ORDER = (
    "access_tokens",
    "account_audit_events",
    "account_qualifying_bans",
    "sessions",
    "session_families",
    "password_credentials",
    "auth_identities",
    "users",
)
E164_PATTERN = re.compile(r"^\+[1-9][0-9]{1,14}$")
REFRESH_DIGEST_PATTERN = re.compile(r"^hmac-sha256:[A-Za-z0-9._-]{1,32}:[0-9a-f]{64}$")


class IdentityProvider(str, Enum):
    PHONE = "phone"
    EMAIL = "email"
    WECHAT = "wechat"
    QQ = "qq"
    APPLE = "apple"


class UserStatus(str, Enum):
    ACTIVE = "active"
    SUSPENDED = "suspended"


class IdentityStatus(str, Enum):
    ACTIVE = "active"
    DETACHED = "detached"


@dataclass(frozen=True)
class VerifiedIdentityClaim:
    provider: IdentityProvider
    provider_subject: str = field(repr=False)
    verification_method: str
    verified_at: int


@dataclass(frozen=True)
class User:
    user_id: str
    status: UserStatus
    created_at: int
    updated_at: int


@dataclass(frozen=True)
class AuthIdentity:
    identity_id: str
    user_id: str
    provider: IdentityProvider
    provider_subject: str = field(repr=False)
    status: IdentityStatus
    verification_method: str
    verified_at: int
    created_at: int
    detached_at: int | None
    replaced_by_identity_id: str | None
    phone_reputation_id: str | None


@dataclass(frozen=True)
class SessionFamily:
    session_family_id: str
    user_id: str
    created_at: int
    revoked_at: int | None
    revocation_reason: str | None


@dataclass(frozen=True)
class Session:
    session_id: str
    user_id: str
    session_family_id: str
    refresh_token_hash: str = field(repr=False)
    created_at: int
    expires_at: int
    revoked_at: int | None
    replaced_by_session_id: str | None


@dataclass(frozen=True)
class AccountAuditEvent:
    event_id: str
    user_id: str
    event_type: str
    subject_id: str | None
    metadata: Mapping[str, Any]
    occurred_at: int


def epoch_ms() -> int:
    return time.time_ns() // 1_000_000


class AccountService:
    def __init__(self, store: AccountStore, *, clock: Callable[[], int] = epoch_ms) -> None:
        self.store = store
        self._clock = clock

    def get_user(self, user_id: str) -> User:
        return _user(self.store.user(user_id))

    def set_user_status(self, user_id: str, status: UserStatus) -> User:
        now = self._clock()
        with self.store.transaction() as tx:
            current = self.store.user(user_id, tx)
            if current["status"] == status.value:
                return _user(current)
            if status is UserStatus.SUSPENDED:
                revoked_families, revoked_sessions = _revoke_all_sessions_tx(
                    tx, user_id, now, "user_suspended"
                )
                event = "user_suspended"
                metadata: Mapping[str, Any] = {
                    "revoked_family_count": revoked_families,
                    "revoked_session_count": revoked_sessions,
                }
            else:
                event, metadata = "user_activated", {}
            tx.execute(
                "UPDATE users SET status=?, updated_at=? WHERE user_id=?",
                (status.value, now, user_id),
            )
            _audit(tx, user_id, event, None, metadata, now)
        return self.get_user(user_id)

    def list_identities(
        self, user_id: str, *, include_detached: bool = False
    ) -> list[AuthIdentity]:
        self.store.user(user_id)
        query = "SELECT * FROM auth_identities WHERE user_id=?"
        values: tuple[object, ...] = (user_id,)
        if not include_detached:
            query += " AND status='active'"
        query += " ORDER BY created_at, identity_id"
        return [_identity(row) for row in self.store.connection.execute(query, values).fetchall()]

    def get_identity(self, identity_id: str) -> AuthIdentity:
        return _identity(self.store.identity(identity_id))

    def find_active_identity(
        self, provider: IdentityProvider, provider_subject: str
    ) -> AuthIdentity | None:
        _validate_subject(provider, provider_subject)
        row = self.store.connection.execute(
            """SELECT * FROM auth_identities
            WHERE provider=? AND provider_subject=? AND status='active'""",
            (provider.value, provider_subject),
        ).fetchone()
        return None if row is None else _identity(row)

    def attach_identity(self, user_id: str, claim: VerifiedIdentityClaim) -> AuthIdentity:
        now = self._clock()
        _validate_claim(claim, now)
        if claim.provider is IdentityProvider.PHONE:
            raise ContractError("phone identities must use replace_phone")
        identity_id = "idn_" + uuid.uuid4().hex
        try:
            with self.store.transaction() as tx:
                _require_auth_eligible_user_tx(tx, user_id)
                existing = _active_identity(tx, claim.provider, claim.provider_subject)
                if existing is not None:
                    if existing["user_id"] == user_id:
                        return _identity(existing)
                    raise IdentityConflictError("verified identity is already active")
                _insert_identity(tx, identity_id, user_id, claim, now)
                _audit(
                    tx,
                    user_id,
                    "identity_linked",
                    identity_id,
                    {"provider": claim.provider.value},
                    now,
                )
        except sqlite3.IntegrityError as exc:
            raise IdentityConflictError("verified identity is already active") from exc
        return self.get_identity(identity_id)

    def create_session(self, user_id: str, *, refresh_token_hash: str, expires_at: int) -> Session:
        now = self._clock()
        _validate_refresh_digest(refresh_token_hash)
        if expires_at <= now:
            raise ContractError("Session expiry must be in the future")
        family_id = "sfm_" + uuid.uuid4().hex
        session_id = "ses_" + uuid.uuid4().hex
        try:
            with self.store.transaction() as tx:
                _require_active_user(self.store.user(user_id, tx))
                active = _active_families(tx, user_id, now)
                while len(active) >= MAX_ACTIVE_SESSION_FAMILIES:
                    evicted = active.pop(0)
                    _revoke_family_tx(
                        tx, str(evicted["session_family_id"]), now, "session_limit_eviction"
                    )
                    _audit(
                        tx,
                        user_id,
                        "session_family_evicted",
                        str(evicted["session_family_id"]),
                        {},
                        now,
                    )
                tx.execute(
                    "INSERT INTO session_families VALUES (?, ?, ?, NULL, NULL)",
                    (family_id, user_id, now),
                )
                tx.execute(
                    "INSERT INTO sessions VALUES (?, ?, ?, ?, ?, ?, NULL, NULL)",
                    (session_id, user_id, family_id, refresh_token_hash, now, expires_at),
                )
                _audit(tx, user_id, "session_created", session_id, {}, now)
        except sqlite3.IntegrityError as exc:
            raise SessionStateError("Session identity conflicts with persisted state") from exc
        return self.get_session(session_id)

    def get_session(self, session_id: str) -> Session:
        return _session(self.store.session(session_id))

    def find_session_by_refresh_hash(self, refresh_token_hash: str) -> Session | None:
        _validate_refresh_digest(refresh_token_hash)
        row = _session_by_refresh_hash(self.store.connection, refresh_token_hash)
        return None if row is None else _session(row)

    def find_active_session_by_refresh_hash(self, refresh_token_hash: str) -> Session | None:
        _validate_refresh_digest(refresh_token_hash)
        row = _session_by_refresh_hash(self.store.connection, refresh_token_hash)
        if row is None:
            return None
        user = self.store.user(str(row["user_id"]))
        family = self.store.session_family(str(row["session_family_id"]))
        phone_status = _active_phone_reputation_status(self.store.connection, str(row["user_id"]))
        return (
            _session(row)
            if session_is_active(
                session_revoked_at=row["revoked_at"],
                session_replaced_by_session_id=row["replaced_by_session_id"],
                session_expires_at=int(row["expires_at"]),
                family_revoked_at=family["revoked_at"],
                family_created_at=int(family["created_at"]),
                user_status=str(user["status"]),
                phone_status=phone_status,
                now=self._clock(),
            )
            else None
        )

    def session_is_active(self, session_id: str) -> bool:
        row = self.store.session(session_id)
        user = self.store.user(str(row["user_id"]))
        family = self.store.session_family(str(row["session_family_id"]))
        phone_status = _active_phone_reputation_status(self.store.connection, str(row["user_id"]))
        return session_is_active(
            session_revoked_at=row["revoked_at"],
            session_replaced_by_session_id=row["replaced_by_session_id"],
            session_expires_at=int(row["expires_at"]),
            family_revoked_at=family["revoked_at"],
            family_created_at=int(family["created_at"]),
            user_status=str(user["status"]),
            phone_status=phone_status,
            now=self._clock(),
        )

    def rotate_session(
        self,
        session_id: str,
        *,
        new_refresh_token_hash: str,
        new_expires_at: int,
    ) -> Session:
        now = self._clock()
        _validate_refresh_digest(new_refresh_token_hash)
        if new_expires_at <= now:
            raise ContractError("Session expiry must be in the future")
        replacement_id = "ses_" + uuid.uuid4().hex
        try:
            with self.store.transaction() as tx:
                old = self.store.session(session_id, tx)
                user = self.store.user(str(old["user_id"]), tx)
                family = self.store.session_family(str(old["session_family_id"]), tx)
                phone_status = _active_phone_reputation_status(tx, str(old["user_id"]))
                if not session_is_active(
                    session_revoked_at=old["revoked_at"],
                    session_replaced_by_session_id=old["replaced_by_session_id"],
                    session_expires_at=int(old["expires_at"]),
                    family_revoked_at=family["revoked_at"],
                    family_created_at=int(family["created_at"]),
                    user_status=str(user["status"]),
                    phone_status=phone_status,
                    now=now,
                ):
                    raise SessionStateError("Session cannot be rotated")
                tx.execute("UPDATE sessions SET revoked_at=? WHERE session_id=?", (now, session_id))
                tx.execute(
                    """UPDATE access_tokens SET revoked_at=?
                    WHERE session_id=? AND revoked_at IS NULL""",
                    (now, session_id),
                )
                tx.execute(
                    "INSERT INTO sessions VALUES (?, ?, ?, ?, ?, ?, NULL, NULL)",
                    (
                        replacement_id,
                        old["user_id"],
                        old["session_family_id"],
                        new_refresh_token_hash,
                        now,
                        new_expires_at,
                    ),
                )
                tx.execute(
                    "UPDATE sessions SET replaced_by_session_id=? WHERE session_id=?",
                    (replacement_id, session_id),
                )
                _audit(tx, str(old["user_id"]), "session_rotated", replacement_id, {}, now)
        except sqlite3.IntegrityError as exc:
            raise SessionStateError("Session rotation conflicts with persisted state") from exc
        return self.get_session(replacement_id)

    def revoke_session(self, session_id: str, *, reason: str = "user_request") -> Session:
        reason = _validate_session_revocation_reason(reason)
        now = self._clock()
        with self.store.transaction() as tx:
            row = self.store.session(session_id, tx)
            if row["revoked_at"] is None:
                tx.execute("UPDATE sessions SET revoked_at=? WHERE session_id=?", (now, session_id))
                _audit(
                    tx,
                    str(row["user_id"]),
                    "session_revoked",
                    session_id,
                    {"reason": reason},
                    now,
                )
        return self.get_session(session_id)

    def revoke_session_family(self, session_family_id: str, *, reason: str = "user_request") -> int:
        reason = _validate_session_revocation_reason(reason)
        now = self._clock()
        with self.store.transaction() as tx:
            family = self.store.session_family(session_family_id, tx)
            if reason == "refresh_token_reuse":
                _audit(
                    tx,
                    str(family["user_id"]),
                    "token_reuse_detected",
                    session_family_id,
                    {},
                    now,
                )
            family_changed, session_count = _revoke_family_tx(tx, session_family_id, now, reason)
            if family_changed:
                _audit(
                    tx,
                    str(family["user_id"]),
                    "session_family_revoked",
                    session_family_id,
                    {"reason": reason, "revoked_session_count": session_count},
                    now,
                )
        return session_count

    def revoke_all_sessions(self, user_id: str, *, reason: str = "user_request") -> int:
        reason = _validate_session_revocation_reason(reason)
        now = self._clock()
        with self.store.transaction() as tx:
            self.store.user(user_id, tx)
            family_count, session_count = _revoke_all_sessions_tx(tx, user_id, now, reason)
            if family_count or session_count:
                _audit(
                    tx,
                    user_id,
                    "all_sessions_revoked",
                    None,
                    {
                        "reason": reason,
                        "revoked_family_count": family_count,
                        "revoked_session_count": session_count,
                    },
                    now,
                )
        return session_count

    def list_audit_events(self, user_id: str) -> list[AccountAuditEvent]:
        self.store.user(user_id)
        rows = self.store.connection.execute(
            """SELECT * FROM account_audit_events WHERE user_id=?
            ORDER BY occurred_at, event_id""",
            (user_id,),
        ).fetchall()
        return [_audit_event(row) for row in rows]

    def erase_account(self, user_id: str) -> None:
        if frozenset(ACCOUNT_ERASURE_ORDER) != ACCOUNT_OWNED_TABLES:
            raise AccountStateError("Account erasure coverage does not match owned tables")
        with self.store.transaction() as tx:
            user = self.store.user(user_id, tx)
            _require_active_user(user)
            for table in ACCOUNT_ERASURE_ORDER:
                tx.execute(f"DELETE FROM {table} WHERE user_id=?", (user_id,))
            for table in ACCOUNT_ERASURE_ORDER:
                if tx.execute(f"SELECT 1 FROM {table} WHERE user_id=?", (user_id,)).fetchone():
                    raise AccountStateError("Account erasure did not cover every owned table")


def _validate_claim(claim: VerifiedIdentityClaim, now: int) -> None:
    _validate_subject(claim.provider, claim.provider_subject)
    if (
        not claim.verification_method
        or claim.verification_method != claim.verification_method.strip()
    ):
        raise ContractError("identity verification method is invalid")
    if len(claim.verification_method.encode("utf-8")) > 128:
        raise ContractError("identity verification method is too long")
    if claim.verified_at < 0 or claim.verified_at > now:
        raise ContractError("identity verification timestamp is invalid")


def _validate_subject(provider: IdentityProvider, subject: str) -> None:
    if not subject or subject != subject.strip() or len(subject.encode("utf-8")) > 512:
        raise ContractError("provider subject is invalid")
    if provider is IdentityProvider.PHONE and not E164_PATTERN.fullmatch(subject):
        raise ContractError("phone provider subject must be canonical E.164")


def _validate_refresh_digest(value: str) -> None:
    if not REFRESH_DIGEST_PATTERN.fullmatch(value):
        raise ContractError("refresh token digest does not match the Account contract")


def _validate_session_revocation_reason(reason: str) -> str:
    if reason not in SESSION_REVOCATION_REASONS:
        raise ContractError("Session revocation reason is not allowed")
    return reason


def _session_by_refresh_hash(
    connection: sqlite3.Connection, refresh_token_hash: str
) -> sqlite3.Row | None:
    return cast(
        sqlite3.Row | None,
        connection.execute(
            "SELECT * FROM sessions WHERE refresh_token_hash=?", (refresh_token_hash,)
        ).fetchone(),
    )


def session_is_active(
    *,
    session_revoked_at: int | None,
    session_replaced_by_session_id: str | None,
    session_expires_at: int,
    family_revoked_at: int | None,
    family_created_at: int,
    user_status: str,
    phone_status: str | None,
    now: int,
) -> bool:
    """Return the sole authoritative v0.6.1 Session validity decision."""

    return (
        session_revoked_at is None
        and session_replaced_by_session_id is None
        and now < session_expires_at
        and family_revoked_at is None
        and now < family_created_at + SESSION_FAMILY_ABSOLUTE_TTL_MS
        and user_status == UserStatus.ACTIVE.value
        and phone_status == "normal"
    )


def _insert_identity(
    tx: sqlite3.Connection,
    identity_id: str,
    user_id: str,
    claim: VerifiedIdentityClaim,
    now: int,
) -> None:
    tx.execute(
        """INSERT INTO auth_identities
        VALUES (?, ?, ?, ?, 'active', ?, ?, ?, NULL, NULL, NULL)""",
        (
            identity_id,
            user_id,
            claim.provider.value,
            claim.provider_subject,
            claim.verification_method,
            claim.verified_at,
            now,
        ),
    )


def _active_identity(
    tx: sqlite3.Connection, provider: IdentityProvider, subject: str
) -> sqlite3.Row | None:
    return cast(
        sqlite3.Row | None,
        tx.execute(
            """SELECT * FROM auth_identities
            WHERE provider=? AND provider_subject=? AND status='active'""",
            (provider.value, subject),
        ).fetchone(),
    )


def _assert_identity_available(
    tx: sqlite3.Connection,
    provider: IdentityProvider,
    subject: str,
    same_user_id: str | None,
) -> None:
    existing = _active_identity(tx, provider, subject)
    if existing is not None and existing["user_id"] != same_user_id:
        raise IdentityConflictError("verified identity is already active")


def _require_active_user(row: sqlite3.Row) -> None:
    if row["status"] != UserStatus.ACTIVE.value:
        raise AccountStateError("suspended User cannot perform this operation")


def _require_auth_eligible_user_tx(tx: sqlite3.Connection, user_id: str) -> None:
    row = tx.execute(
        """SELECT u.status, p.user_id AS password_user_id, r.status AS phone_status
        FROM users u
        LEFT JOIN password_credentials p ON p.user_id=u.user_id
        LEFT JOIN auth_identities i ON i.user_id=u.user_id
            AND i.provider='phone' AND i.status='active'
        LEFT JOIN phone_reputations r ON r.phone_reputation_id=i.phone_reputation_id
        WHERE u.user_id=?""",
        (user_id,),
    ).fetchone()
    if row is None:
        raise AccountStateError("unknown User")
    _require_active_user(cast(sqlite3.Row, row))
    if row["password_user_id"] is None or row["phone_status"] != "normal":
        raise AccountStateError("User is not eligible to create a Session")


def _active_phone_reputation_status(tx: sqlite3.Connection, user_id: str) -> str | None:
    row = tx.execute(
        """SELECT r.status FROM auth_identities i
        JOIN phone_reputations r ON r.phone_reputation_id=i.phone_reputation_id
        WHERE i.user_id=? AND i.provider='phone' AND i.status='active'""",
        (user_id,),
    ).fetchone()
    return None if row is None else str(row["status"])


def _active_families(tx: sqlite3.Connection, user_id: str, now: int) -> list[sqlite3.Row]:
    return cast(
        list[sqlite3.Row],
        tx.execute(
            """SELECT f.* FROM session_families f
            WHERE f.user_id=? AND f.revoked_at IS NULL
              AND f.created_at+?>?
              AND EXISTS (
                SELECT 1 FROM sessions s
                WHERE s.session_family_id=f.session_family_id
                  AND s.revoked_at IS NULL
                  AND s.replaced_by_session_id IS NULL
                  AND s.expires_at>?
              )
            ORDER BY f.created_at, f.session_family_id""",
            (user_id, SESSION_FAMILY_ABSOLUTE_TTL_MS, now, now),
        ).fetchall(),
    )


def _revoke_family_tx(
    tx: sqlite3.Connection, session_family_id: str, now: int, reason: str
) -> tuple[bool, int]:
    family_cursor = tx.execute(
        """UPDATE session_families SET revoked_at=?, revocation_reason=?
        WHERE session_family_id=? AND revoked_at IS NULL""",
        (now, reason, session_family_id),
    )
    cursor = tx.execute(
        """UPDATE sessions SET revoked_at=?
        WHERE session_family_id=? AND revoked_at IS NULL""",
        (now, session_family_id),
    )
    tx.execute(
        """UPDATE access_tokens SET revoked_at=? WHERE session_id IN (
            SELECT session_id FROM sessions WHERE session_family_id=?
        ) AND revoked_at IS NULL""",
        (now, session_family_id),
    )
    return family_cursor.rowcount > 0, int(cursor.rowcount)


def _revoke_all_sessions_tx(
    tx: sqlite3.Connection, user_id: str, now: int, reason: str
) -> tuple[int, int]:
    family_cursor = tx.execute(
        """UPDATE session_families SET revoked_at=?, revocation_reason=?
        WHERE user_id=? AND revoked_at IS NULL""",
        (now, reason, user_id),
    )
    cursor = tx.execute(
        "UPDATE sessions SET revoked_at=? WHERE user_id=? AND revoked_at IS NULL",
        (now, user_id),
    )
    tx.execute(
        "UPDATE access_tokens SET revoked_at=? WHERE user_id=? AND revoked_at IS NULL",
        (now, user_id),
    )
    return int(family_cursor.rowcount), int(cursor.rowcount)


def _audit(
    tx: sqlite3.Connection,
    user_id: str,
    event_type: str,
    subject_id: str | None,
    metadata: Mapping[str, Any],
    now: int,
) -> None:
    if any(key in metadata for key in ("phone", "email", "provider_subject", "token", "secret")):
        raise ContractError("sensitive identity data is forbidden in Account audit metadata")
    tx.execute(
        "INSERT INTO account_audit_events VALUES (?, ?, ?, ?, ?, ?)",
        (
            "evt_" + uuid.uuid4().hex,
            user_id,
            event_type,
            subject_id,
            json.dumps(dict(metadata), sort_keys=True, separators=(",", ":")),
            now,
        ),
    )


def _user(row: sqlite3.Row) -> User:
    return User(
        str(row["user_id"]),
        UserStatus(str(row["status"])),
        int(row["created_at"]),
        int(row["updated_at"]),
    )


def _identity(row: sqlite3.Row) -> AuthIdentity:
    return AuthIdentity(
        str(row["identity_id"]),
        str(row["user_id"]),
        IdentityProvider(str(row["provider"])),
        str(row["provider_subject"]),
        IdentityStatus(str(row["status"])),
        str(row["verification_method"]),
        int(row["verified_at"]),
        int(row["created_at"]),
        None if row["detached_at"] is None else int(row["detached_at"]),
        None if row["replaced_by_identity_id"] is None else str(row["replaced_by_identity_id"]),
        None if row["phone_reputation_id"] is None else str(row["phone_reputation_id"]),
    )


def _session(row: sqlite3.Row) -> Session:
    return Session(
        str(row["session_id"]),
        str(row["user_id"]),
        str(row["session_family_id"]),
        str(row["refresh_token_hash"]),
        int(row["created_at"]),
        int(row["expires_at"]),
        None if row["revoked_at"] is None else int(row["revoked_at"]),
        None if row["replaced_by_session_id"] is None else str(row["replaced_by_session_id"]),
    )


def _audit_event(row: sqlite3.Row) -> AccountAuditEvent:
    raw = json.loads(str(row["metadata_json"]))
    if not isinstance(raw, dict):
        raise AccountStateError("Account audit metadata is not an object")
    return AccountAuditEvent(
        str(row["event_id"]),
        str(row["user_id"]),
        str(row["event_type"]),
        None if row["subject_id"] is None else str(row["subject_id"]),
        cast(Mapping[str, Any], raw),
        int(row["occurred_at"]),
    )
