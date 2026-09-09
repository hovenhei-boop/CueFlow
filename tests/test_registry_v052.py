from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from cueflow.errors import IntegrityError, SourceMissingError
from cueflow.project import ProjectContext
from cueflow.registry import REGISTRY_SCHEMA_VERSION, Registry


@pytest.mark.parametrize("version", [6, 8, 9, 10, 11, 12])
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


def test_new_registry_contains_only_current_contract_tables(tmp_path: Path) -> None:
    context = ProjectContext.create(tmp_path / "project", "fixture")
    try:
        assert REGISTRY_SCHEMA_VERSION == 13
        assert "run_checkpoints" in context.registry._table_names()
        names = context.registry._table_names()
        assert "lexicon_entries" not in names
        assert "reference_assets" not in names
        assert "invocations" in names
        assert "chunk_id" not in context.registry._table_columns("invocations")
        assert "requested_model" in context.registry._table_columns("invocations")
        assert "prompt_sha256" in context.registry._table_columns("invocations")
        assert "diagnostic_json" in context.registry._table_columns("invocations")
    finally:
        context.close()


def test_source_identity_is_normalized_absolute_path_not_filename(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first_dir = tmp_path / "A"
    second_dir = tmp_path / "B"
    first_dir.mkdir()
    second_dir.mkdir()
    first = first_dir / "video.mp4"
    second = second_dir / "video.mp4"
    first.write_bytes(b"same")
    second.write_bytes(b"same")
    context = ProjectContext.create(tmp_path / "project", "fixture")
    monkeypatch.chdir(tmp_path)
    try:
        first_row = context.register_external_asset(
            Path("A") / "." / "video.mp4", asset_kind="media"
        )
        same_row = context.register_external_asset(first.resolve(), asset_kind="media")
        second_row = context.register_external_asset(second, asset_kind="media")
        assert first_row["source_asset_id"] == same_row["source_asset_id"]
        assert first_row["source_asset_id"] != second_row["source_asset_id"]
        assert first_row["filename"] == second_row["filename"] == "video.mp4"
        assert Path(first_row["storage_locator"]).is_absolute()
    finally:
        context.close()


def test_windows_source_identity_is_case_insensitive(
    tmp_path: Path,
) -> None:
    if __import__("os").name != "nt":
        pytest.skip("Windows path identity contract")
    source = tmp_path / "Video.MP4"
    source.write_bytes(b"media")
    context = ProjectContext.create(tmp_path / "project", "fixture")
    try:
        first = context.register_external_asset(source, asset_kind="media")
        second = context.register_external_asset(Path(str(source).swapcase()), asset_kind="media")
        assert first["source_asset_id"] == second["source_asset_id"]
    finally:
        context.close()


def test_missing_registered_path_is_not_replaced_by_same_filename_elsewhere(
    tmp_path: Path,
) -> None:
    registered_dir = tmp_path / "registered"
    alternate_dir = tmp_path / "alternate"
    registered_dir.mkdir()
    alternate_dir.mkdir()
    registered = registered_dir / "video.mp4"
    alternate = alternate_dir / "video.mp4"
    registered.write_bytes(b"first")
    alternate.write_bytes(b"second")
    context = ProjectContext.create(tmp_path / "project", "fixture")
    try:
        row = context.register_external_asset(registered, asset_kind="media")
        registered.unlink()
        with pytest.raises(SourceMissingError, match="source_missing"):
            context.verify_external_asset(str(row["source_asset_id"]))
        assert alternate.is_file()
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
