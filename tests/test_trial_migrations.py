from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from cueflow.errors import TrialStoreError
from cueflow.trial_migrations import (
    TRIAL_SCHEMA_VERSION,
    TRIAL_TABLE_COLUMNS,
    initialize_trial_database,
    validate_trial_schema,
)
from cueflow.trial_store import TrialStore


def test_trial_database_is_explicit_current_and_independent(tmp_path: Path) -> None:
    path = (tmp_path / "state" / "trial.sqlite3").resolve()
    initialize_trial_database(path)

    store = TrialStore(path)
    try:
        assert store.connection.execute(
            "SELECT schema_version FROM trial_schema"
        ).fetchone()[0] == TRIAL_SCHEMA_VERSION
        tables = {
            row[0] for row in store.connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert set(TRIAL_TABLE_COLUMNS).issubset(tables)
        assert "users" not in tables
        assert "runs" not in tables
    finally:
        store.close()


def test_trial_database_refuses_implicit_creation_and_schema_drift(tmp_path: Path) -> None:
    path = (tmp_path / "missing.sqlite3").resolve()
    with pytest.raises(TrialStoreError, match="initialized"):
        TrialStore(path)

    path.touch()
    connection = sqlite3.connect(path)
    try:
        with pytest.raises(sqlite3.OperationalError):
            validate_trial_schema(connection)
    finally:
        connection.close()
