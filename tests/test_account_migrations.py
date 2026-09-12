from __future__ import annotations

import re
import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest

import cueflow.account_migrations as migration_module
from cueflow.account_migration_lock import account_migration_lock
from cueflow.account_migrations import (
    ACCOUNT_MIGRATIONS,
    ACCOUNT_SCHEMA_TABLES,
    MIGRATION_001,
    MIGRATION_002,
    PHONE_REPUTATION_TABLES,
    SESSION_REVOCATION_REASONS,
    Migration,
    migrate_account_database,
    read_account_schema_version,
)
from cueflow.account_store import AccountStore
from cueflow.errors import AccountMigrationError, AccountMigrationLockedError
from tests.account_helpers import make_auth_stack, make_test_account


def _create_v1_database(path: Path, *, nonempty_table: str | None = None) -> None:
    with sqlite3.connect(path) as connection:
        for statement in MIGRATION_001.statements:
            connection.execute(statement)
        connection.execute(
            "INSERT INTO account_schema_migrations VALUES (1, ?, ?, 1)",
            (MIGRATION_001.name, MIGRATION_001.checksum),
        )
        if nonempty_table == "users":
            connection.execute("INSERT INTO users VALUES ('usr_v1', 'active', 1, 1)")
        elif nonempty_table == "auth_identities":
            connection.execute(
                """INSERT INTO auth_identities VALUES (
                    'idn_v1', 'missing_user', 'phone', '+8613810000999', 'active',
                    'test', 1, 1, NULL, NULL
                )"""
            )
        elif nonempty_table == "session_families":
            connection.execute(
                "INSERT INTO session_families VALUES ('sfm_v1', 'missing_user', 1, NULL, NULL)"
            )
        elif nonempty_table == "sessions":
            connection.execute(
                """INSERT INTO sessions VALUES (
                    'ses_v1', 'missing_user', 'missing_family',
                    'hmac-sha256:v1:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
                    1, 2, NULL, NULL
                )"""
            )
        elif nonempty_table == "account_audit_events":
            connection.execute(
                """INSERT INTO account_audit_events VALUES (
                    'evt_v1', 'missing_user', 'test', NULL, '{}', 1
                )"""
            )


def _logical_dump(path: Path) -> tuple[str, ...]:
    with sqlite3.connect(path) as connection:
        return tuple(connection.iterdump())


def test_frozen_migration_checksums_match_only_their_sql_payloads() -> None:
    assert MIGRATION_001.actual_checksum() == MIGRATION_001.checksum
    assert MIGRATION_002.actual_checksum() == MIGRATION_002.checksum
    replacement = replace(MIGRATION_002, guard=lambda _connection: None)
    assert replacement.actual_checksum() == MIGRATION_002.checksum


def test_initial_migration_uses_ledger_without_user_version(tmp_path: Path) -> None:
    database = (tmp_path / "account.sqlite3").resolve()
    backups = (tmp_path / "backups").resolve()

    report = migrate_account_database(database, backups, now_ms=1_000)

    assert report.from_version == 0
    assert report.to_version == 2
    assert report.backup_path is None
    assert report.applied == ("initial_account_core", "phone_password_authentication")
    with sqlite3.connect(database) as connection:
        assert read_account_schema_version(connection) == 2
        assert connection.execute("PRAGMA user_version").fetchone() == (0,)
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        }
        assert tables == ACCOUNT_SCHEMA_TABLES


def test_nonempty_database_without_ledger_fails_closed(tmp_path: Path) -> None:
    database = (tmp_path / "account.sqlite3").resolve()
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE unknown_state (value TEXT)")

    with pytest.raises(AccountMigrationError, match="no migration ledger"):
        migrate_account_database(database, (tmp_path / "backups").resolve())


@pytest.mark.parametrize(
    "nonempty_table",
    ("users", "auth_identities", "session_families", "sessions", "account_audit_events"),
)
def test_migration_002_nonempty_v1_guard_preserves_source_and_verified_backup(
    tmp_path: Path, nonempty_table: str
) -> None:
    database = (tmp_path / f"{nonempty_table}.sqlite3").resolve()
    backups = (tmp_path / "backups").resolve()
    _create_v1_database(database, nonempty_table=nonempty_table)
    before = _logical_dump(database)
    expected_backup = backups / f"{nonempty_table}.v1.2000.sqlite3"

    with pytest.raises(AccountMigrationError) as caught:
        migrate_account_database(database, backups, now_ms=2_000)

    message = str(caught.value)
    assert "nonempty v1 development Account database is unsupported" in message
    assert "delete and recreate it explicitly" in message
    assert str(expected_backup) in message
    assert _logical_dump(database) == before
    assert expected_backup.is_file()
    with sqlite3.connect(expected_backup) as backup:
        assert read_account_schema_version(backup) == 1
        assert backup.execute("PRAGMA integrity_check").fetchone() == ("ok",)


def test_empty_v1_database_advances_to_v2_after_verified_backup(tmp_path: Path) -> None:
    database = (tmp_path / "account.sqlite3").resolve()
    backups = (tmp_path / "backups").resolve()
    _create_v1_database(database)

    report = migrate_account_database(database, backups, now_ms=2_000)

    assert report.from_version == 1
    assert report.to_version == 2
    assert report.backup_path == backups / "account.v1.2000.sqlite3"
    assert report.applied == ("phone_password_authentication",)
    AccountStore(database).close()


def test_failed_forward_migration_keeps_v2_and_verified_backup(tmp_path: Path) -> None:
    database = (tmp_path / "account.sqlite3").resolve()
    backups = (tmp_path / "backups").resolve()
    migrate_account_database(database, backups, now_ms=1_000)
    statements = (
        "CREATE TABLE migration_should_rollback (value TEXT)",
        "THIS IS NOT VALID SQL",
    )
    broken = Migration(2, 3, "broken_test_migration", statements, "placeholder")
    broken = replace(broken, checksum=broken.actual_checksum())

    with pytest.raises(AccountMigrationError, match="migration failed"):
        migrate_account_database(
            database,
            backups,
            target_version=3,
            migrations=(*ACCOUNT_MIGRATIONS, broken),
            now_ms=2_000,
        )

    backup_path = backups / "account.v2.2000.sqlite3"
    for path in (database, backup_path):
        with sqlite3.connect(path) as connection:
            assert read_account_schema_version(connection) == 2
            assert (
                connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE name='migration_should_rollback'"
                ).fetchone()
                is None
            )
            assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)


def test_backup_creation_failure_does_not_mutate_the_account_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = (tmp_path / "account.sqlite3").resolve()
    backups = (tmp_path / "backups").resolve()
    migrate_account_database(database, backups, now_ms=1_000)
    before = database.read_bytes()
    future = Migration(2, 3, "future_after_backup", (), "placeholder")
    future = replace(future, checksum=future.actual_checksum())

    def fail_backup(_connection: sqlite3.Connection, _destination: Path) -> None:
        raise OSError("simulated backup failure")

    monkeypatch.setattr(migration_module, "_backup_and_verify", fail_backup)
    with pytest.raises(AccountMigrationError, match="migration failed"):
        migrate_account_database(
            database,
            backups,
            target_version=3,
            migrations=(*ACCOUNT_MIGRATIONS, future),
            now_ms=2_000,
        )

    assert database.read_bytes() == before
    with sqlite3.connect(database) as connection:
        assert read_account_schema_version(connection) == 2


def test_successful_forward_migration_backs_up_then_advances_contiguous_ledger(
    tmp_path: Path,
) -> None:
    database = (tmp_path / "account.sqlite3").resolve()
    backups = (tmp_path / "backups").resolve()
    migrate_account_database(database, backups, now_ms=1_000)
    statements = (
        "CREATE TABLE migration_probe (value TEXT)",
        "DROP TABLE migration_probe",
    )
    future = Migration(2, 3, "future_test_migration", statements, "placeholder")
    future = replace(future, checksum=future.actual_checksum())

    report = migrate_account_database(
        database,
        backups,
        target_version=3,
        migrations=(*ACCOUNT_MIGRATIONS, future),
        now_ms=2_000,
    )

    assert report.backup_path == backups / "account.v2.2000.sqlite3"
    assert report.applied == ("future_test_migration",)
    with sqlite3.connect(database) as connection:
        assert (
            read_account_schema_version(connection, migrations=(*ACCOUNT_MIGRATIONS, future)) == 3
        )
    with sqlite3.connect(report.backup_path) as connection:
        assert read_account_schema_version(connection) == 2
    with pytest.raises(AccountMigrationError, match="newer than this CueFlow build"):
        AccountStore(database)


def test_session_revocation_reason_runtime_set_equals_sqlite_check(tmp_path: Path) -> None:
    stack = make_auth_stack(tmp_path)
    try:
        user = make_test_account(stack, "+8613810000300")
        sql = stack.store.connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='session_families'"
        ).fetchone()[0]
        match = re.search(r"revocation_reason IN \((.*?)\)", sql, flags=re.DOTALL)
        assert match is not None
        sqlite_reasons = frozenset(re.findall(r"'([^']+)'", match.group(1)))
        assert sqlite_reasons == SESSION_REVOCATION_REASONS
        for index, reason in enumerate(sorted(SESSION_REVOCATION_REASONS)):
            stack.store.connection.execute(
                "INSERT INTO session_families VALUES (?, ?, 1, 2, ?)",
                (f"sfm_reason_{index}", user.user_id, reason),
            )
        with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint"):
            stack.store.connection.execute(
                "INSERT INTO session_families VALUES ('sfm_bad', ?, 1, 2, 'free_text')",
                (user.user_id,),
            )
    finally:
        stack.store.close()


def test_phone_reputation_schema_contract_registers_exactly_five_tables() -> None:
    assert PHONE_REPUTATION_TABLES == {
        "phone_reputations",
        "phone_reputation_operations",
        "phone_reputation_event_log",
        "phone_sanction_events",
        "phone_status_events",
    }


def test_migration_checksum_and_store_version_fail_closed(tmp_path: Path) -> None:
    database = (tmp_path / "account.sqlite3").resolve()
    migrate_account_database(database, (tmp_path / "backups").resolve())
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE account_schema_migrations SET checksum='sha256:tampered' WHERE version=1"
        )

    with pytest.raises(AccountMigrationError, match="checksum"):
        AccountStore(database)


def test_store_rejects_a_missing_identity_uniqueness_index(tmp_path: Path) -> None:
    database = (tmp_path / "account.sqlite3").resolve()
    migrate_account_database(database, (tmp_path / "backups").resolve())
    with sqlite3.connect(database) as connection:
        connection.execute("DROP INDEX active_identity_subject")

    with pytest.raises(AccountMigrationError, match="indexes"):
        AccountStore(database)


def test_store_rejects_a_half_created_user_without_password_or_reputation(
    tmp_path: Path,
) -> None:
    database = (tmp_path / "account.sqlite3").resolve()
    migrate_account_database(database, (tmp_path / "backups").resolve())
    with sqlite3.connect(database) as connection:
        connection.execute("INSERT INTO users VALUES ('usr_half', 'active', 1, 1)")

    with pytest.raises(AccountMigrationError, match="complete User authentication invariant"):
        AccountStore(database)


def test_account_migration_has_an_independent_fail_fast_os_lock(tmp_path: Path) -> None:
    lock_path = (tmp_path / "account.sqlite3.migration.lock").resolve()
    with account_migration_lock(lock_path):
        with pytest.raises(AccountMigrationLockedError, match="another CueFlow process"):
            with account_migration_lock(lock_path):
                raise AssertionError("the second lock must not be acquired")
