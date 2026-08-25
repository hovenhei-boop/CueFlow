from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

import cueflow.registry as registry_module
from cueflow.canonical import hash_json
from cueflow.errors import IntegrityError, ReferenceMissingError
from cueflow.orchestrator import initialize_project, project_status
from cueflow.project import ProjectContext
from cueflow.reference_assets import register_reference_asset, relocate_references
from cueflow.reference_orchestrator import reference_status
from cueflow.registry import REFERENCE_TABLES, Registry


def _strip_v2_reference_tables(database: Path) -> None:
    with sqlite3.connect(database) as connection:
        for table in (
            "artifact_reference_dependencies",
            "reference_invocation_details",
            "reference_work_items",
            "reference_runs",
            "reference_assets",
        ):
            connection.execute(f"DROP TABLE {table}")
        connection.execute("PRAGMA user_version = 1")


def test_v1_registry_migrates_transactionally_without_changing_source_rows(
    tmp_path: Path,
) -> None:
    root = tmp_path / "project"
    source = tmp_path / "source.wav"
    source.write_bytes(b"source bytes are not inspected by this migration test")
    context = initialize_project(root, "Migration", "LOCAL_PROFILE")
    registered = context.register_external_asset(source, asset_kind="media")
    project_id = context.project_id
    artifact_id = context.current_artifact("effective_glossary").artifact_id
    run_id = context.registry.create_run(
        project_id, {"source_asset_id": registered["source_asset_id"]}, hash_json({})
    )
    invocation_id = context.registry.create_invocation(
        run_id=run_id,
        project_id=project_id,
        operation="fixture",
        logical_operation_key="fixture",
        attempt_number=1,
        provider="fixture",
        model="fixture",
    )
    context.registry.set_invocation_status(invocation_id, "explicit_failure")
    context.registry.set_run_status(run_id, "failed")
    context.close()
    database = root / ".cueflow" / "registry.sqlite3"
    _strip_v2_reference_tables(database)

    registry = Registry(database)
    try:
        assert dict(registry.source_asset(project_id, registered["source_asset_id"])) == registered
        assert registry.run(run_id)["status"] == "failed"
        assert registry.invocation(invocation_id)["status"] == "explicit_failure"
        assert registry.artifact(project_id, artifact_id)["artifact_id"] == artifact_id
        assert registry._table_names().issuperset(REFERENCE_TABLES)
        assert registry._table_columns("reference_assets") == (
            "reference_asset_id",
            "filename",
            "locator",
            "detected_format",
            "media_category",
            "registered_at",
        )
        assert registry._connection.execute("PRAGMA user_version").fetchone()[0] == 2
    finally:
        registry.close()


def test_v1_migration_failure_rolls_back_every_new_table(tmp_path: Path) -> None:
    root = tmp_path / "project"
    context = ProjectContext.create(root, "Rollback", "LOCAL_PROFILE")
    context.close()
    database = root / ".cueflow" / "registry.sqlite3"
    _strip_v2_reference_tables(database)
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE reference_assets (unexpected TEXT)")

    with pytest.raises(IntegrityError, match="unexpected Reference tables"):
        Registry(database)
    with sqlite3.connect(database) as connection:
        assert connection.execute("PRAGMA user_version").fetchone() == (1,)
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        }
        assert tables.intersection(REFERENCE_TABLES) == {"reference_assets"}


def test_v1_migration_sql_failure_rolls_back_prior_ddl(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "project"
    context = ProjectContext.create(root, "Rollback DDL", "LOCAL_PROFILE")
    context.close()
    database = root / ".cueflow" / "registry.sqlite3"
    _strip_v2_reference_tables(database)
    monkeypatch.setattr(
        registry_module,
        "REFERENCE_DDL_STATEMENTS",
        (
            registry_module.REFERENCE_DDL_STATEMENTS[0],
            "CREATE TABLE this is not valid SQL",
        ),
    )

    with pytest.raises(sqlite3.OperationalError):
        Registry(database)
    with sqlite3.connect(database) as connection:
        assert connection.execute("PRAGMA user_version").fetchone() == (1,)
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        }
        assert tables.isdisjoint(REFERENCE_TABLES)


def test_reference_identity_is_exact_filename_and_duplicate_does_not_relocate(
    tmp_path: Path,
) -> None:
    context = ProjectContext.create(tmp_path / "project", "Identity", "LOCAL_PROFILE")
    first_dir = tmp_path / "first"
    second_dir = tmp_path / "second"
    first_dir.mkdir()
    second_dir.mkdir()
    first = first_dir / "Notes.TXT"
    second = second_dir / "Notes.TXT"
    distinct_case = second_dir / "notes.txt"
    first.write_text("first", encoding="utf-8")
    second.write_text("second", encoding="utf-8")
    distinct_case.write_text("case-sensitive", encoding="utf-8")
    try:
        registered = register_reference_asset(context, first)
        duplicate = register_reference_asset(context, second)
        missing_duplicate = register_reference_asset(
            context, tmp_path / "missing-locator" / "Notes.TXT"
        )
        other = register_reference_asset(context, distinct_case)
        assert duplicate == registered
        assert missing_duplicate == registered
        assert duplicate["locator"] == str(first.resolve())
        assert other["reference_asset_id"] != registered["reference_asset_id"]
        assert set(registered) == {
            "reference_asset_id",
            "filename",
            "locator",
            "detected_format",
            "media_category",
            "registered_at",
        }
        assert not any("hash" in name or "size" in name or "mtime" in name for name in registered)
        assert "reference_asset_locations" not in context.registry._table_names()
    finally:
        context.close()


def test_reference_signature_and_container_override_extension_guess(tmp_path: Path) -> None:
    context = ProjectContext.create(tmp_path / "project", "Signatures", "LOCAL_PROFILE")
    pdf_named_text = tmp_path / "manual.txt"
    pdf_named_text.write_bytes(b"%PDF-1.7\nfixture header")
    jpeg_named_binary = tmp_path / "photo.bin"
    jpeg_named_binary.write_bytes(bytes.fromhex("FFD8FF") + b"fixture")
    try:
        pdf = register_reference_asset(context, pdf_named_text)
        image = register_reference_asset(context, jpeg_named_binary)
        assert (pdf["detected_format"], pdf["media_category"]) == ("pdf", "document")
        assert (image["detected_format"], image["media_category"]) == ("jpeg", "image")
    finally:
        context.close()


def test_relocate_only_repairs_missing_exact_direct_children(tmp_path: Path) -> None:
    context = ProjectContext.create(tmp_path / "project", "Relocate", "LOCAL_PROFILE")
    original = tmp_path / "original" / "notes.txt"
    original.parent.mkdir()
    original.write_text("registered", encoding="utf-8")
    readable = tmp_path / "readable.txt"
    readable.write_text("leave me", encoding="utf-8")
    source = tmp_path / "source.txt"
    source.write_text("source", encoding="utf-8")
    registered = register_reference_asset(context, original)
    readable_registered = register_reference_asset(context, readable)
    source_registered = context.register_external_asset(source, asset_kind="auxiliary")
    destination = tmp_path / "destination"
    nested = destination / "nested"
    destination.mkdir()
    nested.mkdir()
    (nested / original.name).write_text("must not recurse", encoding="utf-8")
    original.unlink()
    try:
        with pytest.raises(ReferenceMissingError, match="reference relocate"):
            from cueflow.reference_assets import resolve_reference_locator

            resolve_reference_locator(context, registered["reference_asset_id"])
        first_report = relocate_references(context, destination)
        assert first_report["relocated"] == []
        assert first_report["unmatched"][0]["reference_asset_id"] == registered[
            "reference_asset_id"
        ]

        replacement = destination / original.name
        replacement.write_text("direct child", encoding="utf-8")
        report = relocate_references(context, destination)
        assert report["relocated"][0]["locator"] == str(replacement.resolve())
        assert readable_registered["reference_asset_id"] in report[
            "skipped_readable_reference_ids"
        ]
        assert dict(
            context.registry.source_asset(context.project_id, source_registered["source_asset_id"])
        ) == source_registered
    finally:
        context.close()


def test_status_add_and_relocate_do_not_recover_source_or_reference_runs(
    tmp_path: Path,
) -> None:
    context = ProjectContext.create(tmp_path / "project", "Recovery", "LOCAL_PROFILE")
    reference_path = tmp_path / "reference.txt"
    reference_path.write_text("reference", encoding="utf-8")
    reference = register_reference_asset(context, reference_path)
    source_run = context.registry.create_run(
        context.project_id, {"source_asset_id": "fixture"}, hash_json({"source": True})
    )
    reference_run = context.registry.create_reference_run(
        context.project_id,
        reference["reference_asset_id"],
        {"reference_asset_id": reference["reference_asset_id"]},
        hash_json({"reference": True}),
    )
    context.registry.set_run_status(source_run, "running")
    context.registry.set_run_status(reference_run, "running")
    try:
        assert project_status(context)["latest_source_run"]["status"] == "running"
        assert reference_status(context)["reference_assets"][0]["runs"][0]["status"] == "running"
        register_reference_asset(context, reference_path)
        relocate_references(context, tmp_path)
        assert context.registry.run(source_run)["status"] == "running"
        assert context.registry.run(reference_run)["status"] == "running"

        assert context.registry.recover_running_reference_runs() == [reference_run]
        assert context.registry.run(reference_run)["status"] == "interrupted"
        assert context.registry.run(source_run)["status"] == "running"
        assert context.registry.recover_running_source_runs() == [source_run]
    finally:
        context.close()
