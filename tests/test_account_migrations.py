from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest

import cueflow.account_migrations as migration_module
from cueflow.account_migration_lock import account_migration_lock
from cueflow.account_migrations import (
    ACCOUNT_SCHEMA_TABLES,
    MIGRATION_001,
    Migration,
    migrate_account_database,
    read_account_schema_version,
)
from cueflow.account_store import AccountStore
from cueflow.errors import AccountMigrationError, AccountMigrationLockedError


def test_migration_001_checksum_matches_payload() -> None:
    assert MIGRATION_001.actual_checksum() == MIGRATION_001.checksum


def test_initial_migration_uses_ledger_without_user_version(tmp_path: Path) -> None:
    database = (tmp_path / "account.sqlite3").resolve()
    backups = (tmp_path / "backups").resolve()

    report = migrate_account_database(database, backups, now_ms=1_000)

    assert report.from_version == 0
    assert report.to_version == 1
    assert report.backup_path is None
    assert report.applied == ("initial_account_core",)
    with sqlite3.connect(database) as connection:
        assert read_account_schema_version(connection) == 1
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


def test_failed_forward_migration_keeps_v1_and_verified_backup(tmp_path: Path) -> None:
    database = (tmp_path / "account.sqlite3").resolve()
    backups = (tmp_path / "backups").resolve()
    migrate_account_database(database, backups, now_ms=1_000)
    statements = (
        "CREATE TABLE migration_should_rollback (value TEXT)",
        "THIS IS NOT VALID SQL",
    )
    broken = Migration(1, 2, "broken_test_migration", statements, "placeholder")
    broken = replace(broken, checksum=broken.actual_checksum())

    with pytest.raises(AccountMigrationError, match="migration failed"):
        migrate_account_database(
            database,
            backups,
            target_version=2,
            migrations=(MIGRATION_001, broken),
            now_ms=2_000,
        )

    backup_files = list(backups.glob("account.v1.2000.sqlite3"))
    assert len(backup_files) == 1
    for path in (database, backup_files[0]):
        with sqlite3.connect(path) as connection:
            assert read_account_schema_version(connection) == 1
            assert connection.execute(
                "SELECT 1 FROM sqlite_master WHERE name='migration_should_rollback'"
            ).fetchone() is None
            assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)


def test_backup_creation_failure_does_not_mutate_the_account_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = (tmp_path / "account.sqlite3").resolve()
    backups = (tmp_path / "backups").resolve()
    migrate_account_database(database, backups, now_ms=1_000)
    before = hashlib.sha256(database.read_bytes()).hexdigest()
    statements = (
        "CREATE TABLE migration_should_not_start (value TEXT)",
        "DROP TABLE migration_should_not_start",
    )
    future = Migration(1, 2, "future_after_backup", statements, "placeholder")
    future = replace(future, checksum=future.actual_checksum())

    def fail_backup(_connection: sqlite3.Connection, _destination: Path) -> None:
        raise OSError("simulated backup failure")

    monkeypatch.setattr(migration_module, "_backup_and_verify", fail_backup)
    with pytest.raises(AccountMigrationError, match="migration failed"):
        migrate_account_database(
            database,
            backups,
            target_version=2,
            migrations=(MIGRATION_001, future),
            now_ms=2_000,
        )

    assert hashlib.sha256(database.read_bytes()).hexdigest() == before
    with sqlite3.connect(database) as connection:
        assert read_account_schema_version(connection) == 1
        assert connection.execute(
            "SELECT 1 FROM sqlite_master WHERE name='migration_should_not_start'"
        ).fetchone() is None


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
    future = Migration(1, 2, "future_test_migration", statements, "placeholder")
    future = replace(future, checksum=future.actual_checksum())

    report = migrate_account_database(
        database,
        backups,
        target_version=2,
        migrations=(MIGRATION_001, future),
        now_ms=2_000,
    )

    assert report.backup_path == backups / "account.v1.2000.sqlite3"
    assert report.applied == ("future_test_migration",)
    with sqlite3.connect(database) as connection:
        assert read_account_schema_version(
            connection, migrations=(MIGRATION_001, future)
        ) == 2
    with sqlite3.connect(report.backup_path) as connection:
        assert read_account_schema_version(connection) == 1
    with pytest.raises(AccountMigrationError, match="newer than this CueFlow build"):
        AccountStore(database)


def test_migration_001_enforces_reason_and_replacement_state_checks(tmp_path: Path) -> None:
    database = (tmp_path / "account.sqlite3").resolve()
    migrate_account_database(database, (tmp_path / "backups").resolve())
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("INSERT INTO users VALUES ('usr_test', 'active', 1, 1)")
        connection.execute(
            """INSERT INTO auth_identities VALUES (
                'idn_phone', 'usr_test', 'phone', '+8613810000999', 'active',
                'test', 1, 1, NULL, NULL
            )"""
        )
        with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint"):
            connection.execute(
                "INSERT INTO session_families VALUES ('sfm_bad', 'usr_test', 1, 2, 'free_text')"
            )
        connection.execute(
            "INSERT INTO session_families VALUES ('sfm_good', 'usr_test', 1, NULL, NULL)"
        )
        with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint"):
            connection.execute(
                """INSERT INTO sessions VALUES (
                    'ses_bad', 'usr_test', 'sfm_good',
                    'hmac-sha256:v1:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
                    1, 2, NULL, 'ses_bad'
                )"""
            )


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


def test_account_migration_has_an_independent_fail_fast_os_lock(tmp_path: Path) -> None:
    lock_path = (tmp_path / "account.sqlite3.migration.lock").resolve()
    with account_migration_lock(lock_path):
        with pytest.raises(AccountMigrationLockedError, match="another CueFlow process"):
            with account_migration_lock(lock_path):
                raise AssertionError("the second lock must not be acquired")
