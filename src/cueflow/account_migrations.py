from __future__ import annotations

import hashlib
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

from cueflow.account_migration_lock import account_migration_lock
from cueflow.errors import AccountMigrationError

ACCOUNT_SCHEMA_VERSION = 1
SESSION_REVOCATION_REASONS = frozenset(
    {
        "user_request",
        "refresh_token_reuse",
        "user_suspended",
        "session_limit_eviction",
        "administrative_revoke",
        "logout_all",
    }
)

ACCOUNT_OWNED_TABLES = frozenset(
    {
        "users",
        "auth_identities",
        "session_families",
        "sessions",
        "account_audit_events",
    }
)
ACCOUNT_INFRASTRUCTURE_TABLES = frozenset({"account_schema_migrations"})
ACCOUNT_SCHEMA_TABLES = ACCOUNT_OWNED_TABLES | ACCOUNT_INFRASTRUCTURE_TABLES

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
    "account_audit_events": (
        "event_id",
        "user_id",
        "event_type",
        "subject_id",
        "metadata_json",
        "occurred_at",
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

ACCOUNT_INDEX_SQL = {
    "active_identity_subject": MIGRATION_001_STATEMENTS[3],
    "one_active_phone_per_user": MIGRATION_001_STATEMENTS[4],
    "auth_identities_user_status": MIGRATION_001_STATEMENTS[5],
    "session_families_user_state": MIGRATION_001_STATEMENTS[7],
    "one_live_session_leaf_per_family": MIGRATION_001_STATEMENTS[9],
    "sessions_user_state": MIGRATION_001_STATEMENTS[10],
    "account_audit_user_order": MIGRATION_001_STATEMENTS[12],
}


def _migration_payload(statements: tuple[str, ...]) -> bytes:
    return "\n-- cueflow-account-migration-statement --\n".join(statements).encode("utf-8")


@dataclass(frozen=True)
class Migration:
    from_version: int
    to_version: int
    name: str
    statements: tuple[str, ...]
    checksum: str

    def actual_checksum(self) -> str:
        return "sha256:" + hashlib.sha256(_migration_payload(self.statements)).hexdigest()


MIGRATION_001 = Migration(
    from_version=0,
    to_version=1,
    name="initial_account_core",
    statements=MIGRATION_001_STATEMENTS,
    checksum="sha256:3927e97523338401e2f4df6cd3d9ae03b35731f42c7d45254a1f46afb28b124c",
)
ACCOUNT_MIGRATIONS = (MIGRATION_001,)


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
        LEFT JOIN auth_identities i
          ON i.user_id=u.user_id AND i.provider='phone' AND i.status='active'
        GROUP BY u.user_id HAVING COUNT(i.identity_id) != 1 LIMIT 1"""
    ).fetchone()
    if invalid is not None:
        raise AccountMigrationError("Account database violates the active-phone invariant")


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
