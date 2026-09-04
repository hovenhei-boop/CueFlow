from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from cueflow.errors import IntegrityError
from cueflow.project import ProjectContext
from cueflow.registry import REGISTRY_SCHEMA_VERSION, Registry


@pytest.mark.parametrize("version", [6, 8])
def test_old_registry_is_rejected_without_migration(tmp_path: Path, version: int) -> None:
    database = tmp_path / "registry.sqlite3"
    connection = sqlite3.connect(database)
    connection.execute("CREATE TABLE legacy(value TEXT)")
    connection.execute(f"PRAGMA user_version={version}")
    connection.commit()
    connection.close()
    before = database.read_bytes()
    with pytest.raises(IntegrityError, match="does not migrate older projects"):
        Registry(database)
    assert database.read_bytes() == before


def test_new_registry_contains_only_v052_contract_tables(tmp_path: Path) -> None:
    context = ProjectContext.create(tmp_path / "project", "fixture")
    try:
        assert REGISTRY_SCHEMA_VERSION == 9
        assert "run_checkpoints" in context.registry._table_names()
        names = context.registry._table_names()
        assert "lexicon_entries" not in names
        assert "reference_assets" not in names
        assert "invocations" in names
        assert "chunk_id" not in context.registry._table_columns("invocations")
        assert "requested_model" in context.registry._table_columns("invocations")
        assert "prompt_sha256" in context.registry._table_columns("invocations")
    finally:
        context.close()


def test_current_version_with_wrong_columns_is_rejected(tmp_path: Path) -> None:
    database = tmp_path / "wrong.sqlite3"
    connection = sqlite3.connect(database)
    for table in (
        "projects",
        "source_assets",
        "artifacts",
        "artifact_dependencies",
        "current_pointers",
        "runs",
        "invocations",
        "invocation_inputs",
        "run_checkpoints",
    ):
        connection.execute(f"CREATE TABLE {table}(wrong TEXT)")
    connection.execute(f"PRAGMA user_version={REGISTRY_SCHEMA_VERSION}")
    connection.commit()
    connection.close()
    before = database.read_bytes()
    with pytest.raises(IntegrityError, match="columns do not match"):
        Registry(database)
    assert database.read_bytes() == before
