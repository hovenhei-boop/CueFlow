from __future__ import annotations

import hashlib
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from cueflow.account_migration_lock import account_migration_lock
from cueflow.errors import AccountMigrationError

ACCOUNT_SCHEMA_VERSION = 2
SESSION_REVOCATION_REASONS = frozenset(
    {
        "user_request",
        "refresh_token_reuse",
        "user_suspended",
        "session_limit_eviction",
        "administrative_revoke",
        "logout_all",
        "password_changed",
        "password_reset",
        "phone_changed",
    }
)

ACCOUNT_OWNED_TABLES = frozenset(
    {
        "users",
        "auth_identities",
        "password_credentials",
        "session_families",
        "sessions",
        "access_tokens",
        "account_qualifying_bans",
        "account_audit_events",
    }
)
AUTH_TRANSIENT_TABLES = frozenset(
    {
        "sms_challenges",
        "phone_verification_grants",
        "auth_rate_limit_buckets",
        "auth_security_events",
    }
)
PHONE_REPUTATION_TABLES = frozenset(
    {
        "phone_reputations",
        "phone_reputation_operations",
        "phone_reputation_event_log",
        "phone_sanction_events",
        "phone_status_events",
    }
)
ACCOUNT_INFRASTRUCTURE_TABLES = frozenset({"account_schema_migrations"})
ACCOUNT_SCHEMA_TABLES = (
    ACCOUNT_OWNED_TABLES
    | AUTH_TRANSIENT_TABLES
    | PHONE_REPUTATION_TABLES
    | ACCOUNT_INFRASTRUCTURE_TABLES
)

ACCOUNT_TABLE_COLUMNS = {
    "account_schema_migrations": ("version", "name", "checksum", "applied_at"),
    "users": ("user_id", "status", "created_at", "updated_at"),
    "auth_identities": (
        "identity_id",
        "user_id",
        "provider",
        "provider_subject",
        "status",
        "verification_method",
        "verified_at",
        "created_at",
        "detached_at",
        "replaced_by_identity_id",
        "phone_reputation_id",
    ),
    "password_credentials": (
        "user_id",
        "password_hash",
        "created_at",
        "updated_at",
    ),
    "session_families": (
        "session_family_id",
        "user_id",
        "created_at",
        "revoked_at",
        "revocation_reason",
    ),
    "sessions": (
        "session_id",
        "user_id",
        "session_family_id",
        "refresh_token_hash",
        "created_at",
        "expires_at",
        "revoked_at",
        "replaced_by_session_id",
    ),
    "access_tokens": (
        "access_token_id",
        "user_id",
        "session_id",
        "token_hash",
        "csrf_token_hash",
        "created_at",
        "expires_at",
        "revoked_at",
    ),
    "account_qualifying_bans": (
        "account_qualifying_ban_id",
        "user_id",
        "phone_reputation_id",
        "sanction_event_id",
        "applied_at",
        "overturned_at",
    ),
    "account_audit_events": (
        "event_id",
        "user_id",
        "event_type",
        "subject_id",
        "metadata_json",
        "occurred_at",
    ),
    "sms_challenges": (
        "challenge_id",
        "user_id",
        "phone_key_id",
        "phone_key",
        "purpose",
        "code_hash",
        "status",
        "attempts_remaining",
        "created_at",
        "expires_at",
        "verified_at",
    ),
    "phone_verification_grants": (
        "grant_id",
        "challenge_id",
        "user_id",
        "phone_key_id",
        "phone_key",
        "purpose",
        "grant_key_id",
        "grant_hash",
        "created_at",
        "expires_at",
        "consumed_at",
    ),
    "auth_rate_limit_buckets": (
        "bucket_type",
        "key_id",
        "bucket_key",
        "purpose",
        "window_started_at",
        "attempt_count",
        "cooldown_until",
        "updated_at",
    ),
    "auth_security_events": (
        "event_id",
        "event_type",
        "phone_key_id",
        "phone_key",
        "client_key_id",
        "client_key",
        "ip_key_id",
        "ip_key",
        "created_at",
        "expires_at",
    ),
    "phone_reputations": (
        "phone_reputation_id",
        "reputation_key_id",
        "phone_key",
        "phone_encryption_key_id",
        "phone_encryption_nonce",
        "encrypted_phone",
        "current_generation",
        "qualifying_ban_count",
        "status",
        "blocked_at",
        "last_ban_at",
        "created_at",
        "updated_at",
    ),
    "phone_reputation_operations": (
        "operation_id",
        "phone_reputation_id",
        "generation",
        "operation_type",
        "request_fingerprint",
        "expected_sanction_events",
        "expected_status_events",
        "result_code",
        "actor_type",
        "actor_id",
        "reason_code",
        "created_at",
    ),
    "phone_reputation_event_log": (
        "event_id",
        "phone_reputation_id",
        "generation",
        "sequence_no",
        "stream_type",
        "operation_id",
        "operation_stream_slot",
        "actor_type",
        "actor_id",
        "reason_code",
        "created_at",
    ),
    "phone_sanction_events": (
        "event_id",
        "event_type",
        "count_delta",
        "target_event_id",
    ),
    "phone_status_events": (
        "event_id",
        "event_type",
        "from_status",
        "to_status",
        "caused_by_sanction_event_id",
    ),
}

MIGRATION_001_STATEMENTS = (
    """CREATE TABLE account_schema_migrations (
        version INTEGER PRIMARY KEY CHECK(version > 0),
        name TEXT NOT NULL UNIQUE,
        checksum TEXT NOT NULL,
        applied_at INTEGER NOT NULL CHECK(applied_at >= 0)
    )""",
    """CREATE TABLE users (
        user_id TEXT PRIMARY KEY,
        status TEXT NOT NULL CHECK(status IN ('active', 'suspended')),
        created_at INTEGER NOT NULL CHECK(created_at >= 0),
        updated_at INTEGER NOT NULL CHECK(updated_at >= created_at)
    )""",
    """CREATE TABLE auth_identities (
        identity_id TEXT PRIMARY KEY,
        user_id TEXT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
        provider TEXT NOT NULL CHECK(provider IN ('phone', 'email', 'wechat', 'qq', 'apple')),
        provider_subject TEXT NOT NULL CHECK(length(provider_subject) BETWEEN 1 AND 512),
        status TEXT NOT NULL CHECK(status IN ('active', 'detached')),
        verification_method TEXT NOT NULL CHECK(length(verification_method) BETWEEN 1 AND 128),
        verified_at INTEGER NOT NULL CHECK(verified_at >= 0),
        created_at INTEGER NOT NULL CHECK(created_at >= 0),
        detached_at INTEGER,
        replaced_by_identity_id TEXT REFERENCES auth_identities(identity_id)
            DEFERRABLE INITIALLY DEFERRED,
        CHECK(
            (status = 'active' AND detached_at IS NULL AND replaced_by_identity_id IS NULL)
            OR (status = 'detached' AND detached_at IS NOT NULL)
        )
    )""",
    """CREATE UNIQUE INDEX active_identity_subject
        ON auth_identities(provider, provider_subject) WHERE status = 'active'""",
    """CREATE UNIQUE INDEX one_active_phone_per_user
        ON auth_identities(user_id) WHERE provider = 'phone' AND status = 'active'""",
    """CREATE INDEX auth_identities_user_status
        ON auth_identities(user_id, status, provider, created_at, identity_id)""",
    """CREATE TABLE session_families (
        session_family_id TEXT PRIMARY KEY,
        user_id TEXT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
        created_at INTEGER NOT NULL CHECK(created_at >= 0),
        revoked_at INTEGER,
        revocation_reason TEXT CHECK(
            revocation_reason IS NULL OR revocation_reason IN (
                'user_request',
                'refresh_token_reuse',
                'user_suspended',
                'session_limit_eviction',
                'administrative_revoke',
                'logout_all'
            )
        ),
        UNIQUE(session_family_id, user_id),
        CHECK(
            (revoked_at IS NULL AND revocation_reason IS NULL)
            OR (revoked_at IS NOT NULL AND revocation_reason IS NOT NULL)
        )
    )""",
    """CREATE INDEX session_families_user_state
        ON session_families(user_id, revoked_at, created_at, session_family_id)""",
    """CREATE TABLE sessions (
        session_id TEXT PRIMARY KEY,
        user_id TEXT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
        session_family_id TEXT NOT NULL,
        refresh_token_hash TEXT NOT NULL UNIQUE,
        created_at INTEGER NOT NULL CHECK(created_at >= 0),
        expires_at INTEGER NOT NULL CHECK(expires_at > created_at),
        revoked_at INTEGER,
        replaced_by_session_id TEXT REFERENCES sessions(session_id)
            DEFERRABLE INITIALLY DEFERRED,
        CHECK(replaced_by_session_id IS NULL OR revoked_at IS NOT NULL),
        FOREIGN KEY(session_family_id, user_id)
            REFERENCES session_families(session_family_id, user_id) ON DELETE CASCADE
    )""",
    """CREATE UNIQUE INDEX one_live_session_leaf_per_family
        ON sessions(session_family_id)
        WHERE revoked_at IS NULL AND replaced_by_session_id IS NULL""",
    """CREATE INDEX sessions_user_state
        ON sessions(user_id, revoked_at, expires_at, created_at, session_id)""",
    """CREATE TABLE account_audit_events (
        event_id TEXT PRIMARY KEY,
        user_id TEXT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
        event_type TEXT NOT NULL,
        subject_id TEXT,
        metadata_json TEXT NOT NULL,
        occurred_at INTEGER NOT NULL CHECK(occurred_at >= 0)
    )""",
    """CREATE INDEX account_audit_user_order
        ON account_audit_events(user_id, occurred_at, event_id)""",
)

MIGRATION_002_STATEMENTS = (
    "DROP TABLE sessions",
    "DROP TABLE session_families",
    "DROP TABLE auth_identities",
    """CREATE TABLE session_families (
        session_family_id TEXT PRIMARY KEY,
        user_id TEXT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
        created_at INTEGER NOT NULL CHECK(created_at >= 0),
        revoked_at INTEGER,
        revocation_reason TEXT CHECK(
            revocation_reason IS NULL OR revocation_reason IN (
                'user_request',
                'refresh_token_reuse',
                'user_suspended',
                'session_limit_eviction',
                'administrative_revoke',
                'logout_all',
                'password_changed',
                'password_reset',
                'phone_changed'
            )
        ),
        UNIQUE(session_family_id, user_id),
        CHECK(
            (revoked_at IS NULL AND revocation_reason IS NULL)
            OR (revoked_at IS NOT NULL AND revocation_reason IS NOT NULL)
        )
    )""",
    """CREATE INDEX session_families_user_state
        ON session_families(user_id, revoked_at, created_at, session_family_id)""",
    """CREATE TABLE sessions (
        session_id TEXT PRIMARY KEY,
        user_id TEXT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
        session_family_id TEXT NOT NULL,
        refresh_token_hash TEXT NOT NULL UNIQUE,
        created_at INTEGER NOT NULL CHECK(created_at >= 0),
        expires_at INTEGER NOT NULL CHECK(expires_at > created_at),
        revoked_at INTEGER,
        replaced_by_session_id TEXT REFERENCES sessions(session_id)
            DEFERRABLE INITIALLY DEFERRED,
        CHECK(replaced_by_session_id IS NULL OR revoked_at IS NOT NULL),
        FOREIGN KEY(session_family_id, user_id)
            REFERENCES session_families(session_family_id, user_id) ON DELETE CASCADE
    )""",
    """CREATE UNIQUE INDEX one_live_session_leaf_per_family
        ON sessions(session_family_id)
        WHERE revoked_at IS NULL AND replaced_by_session_id IS NULL""",
    """CREATE INDEX sessions_user_state
        ON sessions(user_id, revoked_at, expires_at, created_at, session_id)""",
    """CREATE TABLE password_credentials (
        user_id TEXT PRIMARY KEY REFERENCES users(user_id) ON DELETE CASCADE,
        password_hash TEXT NOT NULL CHECK(password_hash LIKE '$argon2id$%'),
        created_at INTEGER NOT NULL CHECK(created_at >= 0),
        updated_at INTEGER NOT NULL CHECK(updated_at >= created_at)
    )""",
    """CREATE TABLE access_tokens (
        access_token_id TEXT PRIMARY KEY,
        user_id TEXT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
        session_id TEXT NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE,
        token_hash TEXT NOT NULL UNIQUE,
        csrf_token_hash TEXT NOT NULL,
        created_at INTEGER NOT NULL CHECK(created_at >= 0),
        expires_at INTEGER NOT NULL CHECK(expires_at > created_at),
        revoked_at INTEGER,
        UNIQUE(access_token_id, user_id)
    )""",
    """CREATE INDEX access_tokens_user_state
        ON access_tokens(user_id, revoked_at, expires_at, created_at, access_token_id)""",
    """CREATE INDEX access_tokens_session_state
        ON access_tokens(session_id, revoked_at, expires_at)""",
    """CREATE TABLE phone_reputations (
        phone_reputation_id TEXT PRIMARY KEY,
        reputation_key_id TEXT NOT NULL CHECK(length(reputation_key_id) BETWEEN 1 AND 32),
        phone_key BLOB NOT NULL UNIQUE CHECK(length(phone_key) = 32),
        phone_encryption_key_id TEXT NOT NULL
            CHECK(length(phone_encryption_key_id) BETWEEN 1 AND 32),
        phone_encryption_nonce BLOB NOT NULL CHECK(length(phone_encryption_nonce) = 12),
        encrypted_phone BLOB NOT NULL CHECK(length(encrypted_phone) > 16),
        current_generation INTEGER NOT NULL CHECK(current_generation >= 1),
        qualifying_ban_count INTEGER NOT NULL CHECK(qualifying_ban_count >= 0),
        status TEXT NOT NULL CHECK(status IN ('normal', 'blocked')),
        blocked_at INTEGER,
        last_ban_at INTEGER,
        created_at INTEGER NOT NULL CHECK(created_at >= 0),
        updated_at INTEGER NOT NULL CHECK(updated_at >= created_at),
        UNIQUE(phone_encryption_key_id, phone_encryption_nonce),
        CHECK(
            (status = 'normal' AND blocked_at IS NULL)
            OR (status = 'blocked' AND blocked_at IS NOT NULL)
        )
    )""",
    """CREATE TABLE auth_identities (
        identity_id TEXT PRIMARY KEY,
        user_id TEXT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
        provider TEXT NOT NULL CHECK(provider IN ('phone', 'email', 'wechat', 'qq', 'apple')),
        provider_subject TEXT NOT NULL CHECK(length(provider_subject) BETWEEN 1 AND 512),
        status TEXT NOT NULL CHECK(status IN ('active', 'detached')),
        verification_method TEXT NOT NULL CHECK(length(verification_method) BETWEEN 1 AND 128),
        verified_at INTEGER NOT NULL CHECK(verified_at >= 0),
        created_at INTEGER NOT NULL CHECK(created_at >= 0),
        detached_at INTEGER,
        replaced_by_identity_id TEXT REFERENCES auth_identities(identity_id)
            DEFERRABLE INITIALLY DEFERRED,
        phone_reputation_id TEXT REFERENCES phone_reputations(phone_reputation_id)
            ON DELETE RESTRICT,
        CHECK(
            (status = 'active' AND detached_at IS NULL AND replaced_by_identity_id IS NULL)
            OR (status = 'detached' AND detached_at IS NOT NULL)
        ),
        CHECK(
            (provider = 'phone' AND phone_reputation_id IS NOT NULL)
            OR (provider != 'phone' AND phone_reputation_id IS NULL)
        )
    )""",
    """CREATE UNIQUE INDEX active_identity_subject
        ON auth_identities(provider, provider_subject) WHERE status = 'active'""",
    """CREATE UNIQUE INDEX one_active_phone_per_user
        ON auth_identities(user_id) WHERE provider = 'phone' AND status = 'active'""",
    """CREATE INDEX auth_identities_user_status
        ON auth_identities(user_id, status, provider, created_at, identity_id)""",
    """CREATE TABLE phone_reputation_operations (
        operation_id TEXT PRIMARY KEY,
        phone_reputation_id TEXT NOT NULL
            REFERENCES phone_reputations(phone_reputation_id) ON DELETE RESTRICT,
        generation INTEGER NOT NULL CHECK(generation >= 1),
        operation_type TEXT NOT NULL CHECK(operation_type IN (
            'generation_started',
            'qualifying_ban',
            'administrative_block',
            'administrative_unblock',
            'overturn_sanction',
            'phone_reassignment'
        )),
        request_fingerprint BLOB NOT NULL CHECK(length(request_fingerprint) = 32),
        expected_sanction_events INTEGER NOT NULL
            CHECK(expected_sanction_events IN (0, 1)),
        expected_status_events INTEGER NOT NULL CHECK(expected_status_events IN (0, 1)),
        result_code TEXT NOT NULL CHECK(length(result_code) BETWEEN 1 AND 64),
        actor_type TEXT NOT NULL CHECK(actor_type IN ('system', 'admin', 'service')),
        actor_id TEXT NOT NULL CHECK(length(actor_id) BETWEEN 1 AND 128),
        reason_code TEXT NOT NULL CHECK(length(reason_code) BETWEEN 1 AND 128),
        created_at INTEGER NOT NULL CHECK(created_at >= 0)
    )""",
    """CREATE INDEX phone_reputation_operations_order
        ON phone_reputation_operations(
            phone_reputation_id, generation, created_at, operation_id
        )""",
    """CREATE TABLE phone_reputation_event_log (
        event_id TEXT PRIMARY KEY,
        phone_reputation_id TEXT NOT NULL
            REFERENCES phone_reputations(phone_reputation_id) ON DELETE RESTRICT,
        generation INTEGER NOT NULL CHECK(generation >= 1),
        sequence_no INTEGER NOT NULL CHECK(sequence_no >= 1),
        stream_type TEXT NOT NULL CHECK(stream_type IN ('sanction', 'status')),
        operation_id TEXT NOT NULL
            REFERENCES phone_reputation_operations(operation_id) ON DELETE RESTRICT,
        operation_stream_slot INTEGER NOT NULL CHECK(operation_stream_slot IN (1, 2)),
        actor_type TEXT NOT NULL CHECK(actor_type IN ('system', 'admin', 'service')),
        actor_id TEXT NOT NULL CHECK(length(actor_id) BETWEEN 1 AND 128),
        reason_code TEXT NOT NULL CHECK(length(reason_code) BETWEEN 1 AND 128),
        created_at INTEGER NOT NULL CHECK(created_at >= 0),
        UNIQUE(phone_reputation_id, generation, sequence_no),
        UNIQUE(operation_id, operation_stream_slot),
        CHECK(
            (stream_type = 'sanction' AND operation_stream_slot = 1)
            OR (stream_type = 'status' AND operation_stream_slot = 2)
        )
    )""",
    """CREATE INDEX phone_reputation_event_order
        ON phone_reputation_event_log(phone_reputation_id, generation, sequence_no)""",
    """CREATE TABLE phone_sanction_events (
        event_id TEXT PRIMARY KEY
            REFERENCES phone_reputation_event_log(event_id) ON DELETE RESTRICT,
        event_type TEXT NOT NULL CHECK(event_type IN (
            'generation_started',
            'qualifying_ban_applied',
            'qualifying_ban_overturned'
        )),
        count_delta INTEGER NOT NULL CHECK(count_delta IN (-1, 0, 1)),
        target_event_id TEXT REFERENCES phone_sanction_events(event_id) ON DELETE RESTRICT,
        CHECK(
            (event_type = 'generation_started' AND count_delta = 0
                AND target_event_id IS NULL)
            OR (event_type = 'qualifying_ban_applied' AND count_delta = 1
                AND target_event_id IS NULL)
            OR (event_type = 'qualifying_ban_overturned' AND count_delta = -1
                AND target_event_id IS NOT NULL)
        )
    )""",
    """CREATE UNIQUE INDEX one_overturn_per_sanction
        ON phone_sanction_events(target_event_id)
        WHERE event_type = 'qualifying_ban_overturned'""",
    """CREATE TABLE phone_status_events (
        event_id TEXT PRIMARY KEY
            REFERENCES phone_reputation_event_log(event_id) ON DELETE RESTRICT,
        event_type TEXT NOT NULL CHECK(event_type IN (
            'generation_started',
            'phone_blocked',
            'phone_unblocked'
        )),
        from_status TEXT CHECK(from_status IN ('normal', 'blocked')),
        to_status TEXT NOT NULL CHECK(to_status IN ('normal', 'blocked')),
        caused_by_sanction_event_id TEXT
            REFERENCES phone_sanction_events(event_id) ON DELETE RESTRICT,
        CHECK(
            (event_type = 'generation_started' AND from_status IS NULL
                AND to_status = 'normal' AND caused_by_sanction_event_id IS NULL)
            OR (event_type = 'phone_blocked' AND from_status = 'normal'
                AND to_status = 'blocked')
            OR (event_type = 'phone_unblocked' AND from_status = 'blocked'
                AND to_status = 'normal')
        )
    )""",
    """CREATE TABLE account_qualifying_bans (
        account_qualifying_ban_id TEXT PRIMARY KEY,
        user_id TEXT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
        phone_reputation_id TEXT NOT NULL
            REFERENCES phone_reputations(phone_reputation_id) ON DELETE RESTRICT,
        sanction_event_id TEXT NOT NULL UNIQUE
            REFERENCES phone_sanction_events(event_id) ON DELETE RESTRICT,
        applied_at INTEGER NOT NULL CHECK(applied_at >= 0),
        overturned_at INTEGER,
        CHECK(overturned_at IS NULL OR overturned_at >= applied_at)
    )""",
    """CREATE UNIQUE INDEX one_effective_qualifying_ban_per_user
        ON account_qualifying_bans(user_id) WHERE overturned_at IS NULL""",
    """CREATE TABLE sms_challenges (
        challenge_id TEXT PRIMARY KEY,
        user_id TEXT REFERENCES users(user_id) ON DELETE SET NULL,
        phone_key_id TEXT NOT NULL CHECK(length(phone_key_id) BETWEEN 1 AND 32),
        phone_key BLOB NOT NULL CHECK(length(phone_key) = 32),
        purpose TEXT NOT NULL CHECK(purpose IN (
            'phone_continue', 'password_reset', 'phone_change',
            'account_erasure', 'phone_appeal'
        )),
        code_hash BLOB NOT NULL CHECK(length(code_hash) = 32),
        status TEXT NOT NULL CHECK(status IN (
            'pending', 'sent', 'verified', 'failed', 'cancelled', 'expired'
        )),
        attempts_remaining INTEGER NOT NULL CHECK(attempts_remaining BETWEEN 0 AND 5),
        created_at INTEGER NOT NULL CHECK(created_at >= 0),
        expires_at INTEGER NOT NULL CHECK(expires_at > created_at),
        verified_at INTEGER
    )""",
    """CREATE INDEX sms_challenges_phone_purpose
        ON sms_challenges(phone_key_id, phone_key, purpose, created_at, challenge_id)""",
    """CREATE TABLE phone_verification_grants (
        grant_id TEXT PRIMARY KEY,
        challenge_id TEXT NOT NULL REFERENCES sms_challenges(challenge_id) ON DELETE RESTRICT,
        user_id TEXT REFERENCES users(user_id) ON DELETE SET NULL,
        phone_key_id TEXT NOT NULL CHECK(length(phone_key_id) BETWEEN 1 AND 32),
        phone_key BLOB NOT NULL CHECK(length(phone_key) = 32),
        purpose TEXT NOT NULL CHECK(purpose IN (
            'phone_continue', 'password_reset', 'phone_change',
            'account_erasure', 'phone_appeal'
        )),
        grant_key_id TEXT NOT NULL CHECK(length(grant_key_id) BETWEEN 1 AND 32),
        grant_hash BLOB NOT NULL UNIQUE CHECK(length(grant_hash) = 32),
        created_at INTEGER NOT NULL CHECK(created_at >= 0),
        expires_at INTEGER NOT NULL CHECK(expires_at > created_at),
        consumed_at INTEGER
    )""",
    """CREATE INDEX phone_verification_grants_state
        ON phone_verification_grants(
            phone_key_id, phone_key, purpose, consumed_at, expires_at
        )""",
    """CREATE TABLE auth_rate_limit_buckets (
        bucket_type TEXT NOT NULL CHECK(bucket_type IN ('phone', 'client', 'ip')),
        key_id TEXT NOT NULL CHECK(length(key_id) BETWEEN 1 AND 32),
        bucket_key BLOB NOT NULL CHECK(length(bucket_key) = 32),
        purpose TEXT NOT NULL CHECK(length(purpose) BETWEEN 1 AND 64),
        window_started_at INTEGER NOT NULL CHECK(window_started_at >= 0),
        attempt_count INTEGER NOT NULL CHECK(attempt_count >= 0),
        cooldown_until INTEGER NOT NULL CHECK(cooldown_until >= 0),
        updated_at INTEGER NOT NULL CHECK(updated_at >= window_started_at),
        PRIMARY KEY(bucket_type, key_id, bucket_key, purpose)
    )""",
    """CREATE TABLE auth_security_events (
        event_id TEXT PRIMARY KEY,
        event_type TEXT NOT NULL CHECK(event_type IN (
            'password_failure', 'sms_rate_limited', 'sms_code_failure',
            'token_reuse', 'phone_blocked'
        )),
        phone_key_id TEXT,
        phone_key BLOB CHECK(phone_key IS NULL OR length(phone_key) = 32),
        client_key_id TEXT,
        client_key BLOB CHECK(client_key IS NULL OR length(client_key) = 32),
        ip_key_id TEXT,
        ip_key BLOB CHECK(ip_key IS NULL OR length(ip_key) = 32),
        created_at INTEGER NOT NULL CHECK(created_at >= 0),
        expires_at INTEGER NOT NULL CHECK(expires_at > created_at),
        CHECK((phone_key_id IS NULL) = (phone_key IS NULL)),
        CHECK((client_key_id IS NULL) = (client_key IS NULL)),
        CHECK((ip_key_id IS NULL) = (ip_key IS NULL))
    )""",
    """CREATE INDEX auth_security_events_expiry
        ON auth_security_events(expires_at, created_at, event_id)""",
)


def _index_contract(*payloads: tuple[str, ...]) -> dict[str, str]:
    result: dict[str, str] = {}
    for statements in payloads:
        for statement in statements:
            words = statement.split()
            if len(words) >= 3 and words[0] == "CREATE" and "INDEX" in words[:3]:
                name = words[words.index("INDEX") + 1]
                result[name] = statement
    return result


ACCOUNT_INDEX_SQL = _index_contract(MIGRATION_001_STATEMENTS, MIGRATION_002_STATEMENTS)


def _migration_payload(statements: tuple[str, ...]) -> bytes:
    return "\n-- cueflow-account-migration-statement --\n".join(statements).encode("utf-8")


MigrationGuard = Callable[[sqlite3.Connection], None]


class _MigrationGuardRejected(AccountMigrationError):
    pass


def _require_empty_v1_account_database(connection: sqlite3.Connection) -> None:
    v1_tables = (
        "users",
        "auth_identities",
        "sessions",
        "session_families",
        "account_audit_events",
    )
    for table in v1_tables:
        if connection.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone() is not None:
            raise _MigrationGuardRejected(
                "nonempty v1 development Account database is unsupported; "
                "delete and recreate it explicitly"
            )


@dataclass(frozen=True)
class Migration:
    from_version: int
    to_version: int
    name: str
    statements: tuple[str, ...]
    checksum: str
    guard: MigrationGuard | None = None

    def actual_checksum(self) -> str:
        return "sha256:" + hashlib.sha256(_migration_payload(self.statements)).hexdigest()


MIGRATION_001 = Migration(
    from_version=0,
    to_version=1,
    name="initial_account_core",
    statements=MIGRATION_001_STATEMENTS,
    checksum="sha256:3927e97523338401e2f4df6cd3d9ae03b35731f42c7d45254a1f46afb28b124c",
)
MIGRATION_002 = Migration(
    from_version=1,
    to_version=2,
    name="phone_password_authentication",
    statements=MIGRATION_002_STATEMENTS,
    checksum="sha256:eecfff8a33e1f94af69483a8d42e58db9e26ae1b1afe91411f69a052f10ddc48",
    guard=_require_empty_v1_account_database,
)
ACCOUNT_MIGRATIONS = (MIGRATION_001, MIGRATION_002)


@dataclass(frozen=True)
class MigrationReport:
    from_version: int
    to_version: int
    backup_path: Path | None
    applied: tuple[str, ...]


def migrate_account_database(
    path: Path,
    backup_dir: Path,
    *,
    target_version: int = ACCOUNT_SCHEMA_VERSION,
    migrations: tuple[Migration, ...] = ACCOUNT_MIGRATIONS,
    now_ms: int | None = None,
) -> MigrationReport:
    if not path.is_absolute() or not backup_dir.is_absolute():
        raise AccountMigrationError("Account database and backup paths must be absolute")
    path.parent.mkdir(parents=True, exist_ok=True)
    backup_dir.mkdir(parents=True, exist_ok=True)
    lock_path = Path(str(path) + ".migration.lock")
    with account_migration_lock(lock_path):
        connection = sqlite3.connect(path, timeout=5.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        backup_path: Path | None = None
        try:
            start = read_account_schema_version(connection, migrations=migrations)
            if start > target_version:
                raise AccountMigrationError("Account database is newer than this CueFlow build")
            if start == target_version:
                validate_account_schema(connection)
                validate_account_invariants(connection)
                return MigrationReport(start, target_version, None, ())
            instant = int(time.time_ns() // 1_000_000 if now_ms is None else now_ms)
            if start > 0:
                backup_path = backup_dir / f"{path.stem}.v{start}.{instant}.sqlite3"
                if backup_path.exists():
                    raise AccountMigrationError("Account migration backup path already exists")
                _backup_and_verify(connection, backup_path)
            connection.execute("BEGIN EXCLUSIVE")
            current = read_account_schema_version(connection, migrations=migrations)
            if current != start:
                raise AccountMigrationError("Account schema changed while migration was starting")
            by_source = {migration.from_version: migration for migration in migrations}
            applied: list[str] = []
            while current < target_version:
                migration = by_source.get(current)
                if migration is None or migration.to_version != current + 1:
                    raise AccountMigrationError("Account migration chain is incomplete")
                if migration.actual_checksum() != migration.checksum:
                    raise AccountMigrationError("Account migration payload checksum does not match")
                if migration.guard is not None:
                    migration.guard(connection)
                for statement in migration.statements:
                    connection.execute(statement)
                connection.execute(
                    "INSERT INTO account_schema_migrations VALUES (?, ?, ?, ?)",
                    (migration.to_version, migration.name, migration.checksum, instant),
                )
                current = migration.to_version
                applied.append(migration.name)
            validate_account_schema(connection)
            validate_account_invariants(connection)
            if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
                raise AccountMigrationError("Account migration produced invalid foreign keys")
            connection.commit()
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA busy_timeout=5000")
            return MigrationReport(start, current, backup_path, tuple(applied))
        except _MigrationGuardRejected as exc:
            if connection.in_transaction:
                connection.rollback()
            message = str(exc)
            if backup_path is not None:
                message += f"; a pre-migration backup was left at {backup_path}"
            raise AccountMigrationError(message) from exc
        except BaseException as exc:
            if connection.in_transaction:
                connection.rollback()
            if isinstance(exc, AccountMigrationError):
                raise
            raise AccountMigrationError("Account database migration failed") from exc
        finally:
            connection.close()


def read_account_schema_version(
    connection: sqlite3.Connection,
    *,
    migrations: tuple[Migration, ...] = ACCOUNT_MIGRATIONS,
) -> int:
    tables = _table_names(connection)
    if not tables:
        return 0
    if "account_schema_migrations" not in tables:
        raise AccountMigrationError("Account database has tables but no migration ledger")
    rows = connection.execute(
        "SELECT version, name, checksum FROM account_schema_migrations ORDER BY version"
    ).fetchall()
    if not rows:
        raise AccountMigrationError("Account migration ledger is empty")
    versions = [int(row[0]) for row in rows]
    if versions != list(range(1, versions[-1] + 1)):
        raise AccountMigrationError("Account migration ledger is not contiguous")
    by_target = {migration.to_version: migration for migration in migrations}
    for row in rows:
        version = int(row[0])
        known = by_target.get(version)
        if known is None:
            continue
        if row[1] != known.name or row[2] != known.checksum:
            raise AccountMigrationError("Account migration ledger checksum does not match")
    return versions[-1]


def validate_account_schema(connection: sqlite3.Connection) -> None:
    actual = _table_names(connection)
    if actual != ACCOUNT_SCHEMA_TABLES:
        raise AccountMigrationError("Account database tables do not match the current contract")
    for table, expected in ACCOUNT_TABLE_COLUMNS.items():
        columns = tuple(
            str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})").fetchall()
        )
        if columns != expected:
            raise AccountMigrationError(
                f"Account database columns do not match the current contract for {table}"
            )
    actual_indexes = {
        str(row[0]): " ".join(str(row[1]).split())
        for row in connection.execute(
            """SELECT name, sql FROM sqlite_master
            WHERE type='index' AND name NOT LIKE 'sqlite_%'"""
        ).fetchall()
    }
    expected_indexes = {
        name: " ".join(statement.split()) for name, statement in ACCOUNT_INDEX_SQL.items()
    }
    if actual_indexes != expected_indexes:
        raise AccountMigrationError("Account database indexes do not match the current contract")


def validate_account_invariants(connection: sqlite3.Connection) -> None:
    invalid = connection.execute(
        """SELECT u.user_id FROM users u
        WHERE (
            SELECT COUNT(*) FROM auth_identities i
            JOIN phone_reputations r ON r.phone_reputation_id=i.phone_reputation_id
            WHERE i.user_id=u.user_id AND i.provider='phone' AND i.status='active'
        ) != 1 OR (
            SELECT COUNT(*) FROM password_credentials p WHERE p.user_id=u.user_id
        ) != 1
        LIMIT 1"""
    ).fetchone()
    if invalid is not None:
        raise AccountMigrationError(
            "Account database violates the complete User authentication invariant"
        )


def _backup_and_verify(connection: sqlite3.Connection, destination: Path) -> None:
    backup = sqlite3.connect(destination)
    try:
        connection.backup(backup)
    finally:
        backup.close()
    verification = sqlite3.connect(destination)
    try:
        result = verification.execute("PRAGMA integrity_check").fetchone()
        if result is None or result[0] != "ok":
            raise AccountMigrationError("Account migration backup failed integrity verification")
    finally:
        verification.close()


def _table_names(connection: sqlite3.Connection) -> frozenset[str]:
    return frozenset(
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
    )
