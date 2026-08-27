from __future__ import annotations

from pathlib import Path

import pytest

from cueflow.canonical import hash_json
from cueflow.errors import ReferenceMissingError
from cueflow.orchestrator import project_status
from cueflow.project import ProjectContext
from cueflow.reference_assets import register_reference_asset, relocate_references
from cueflow.reference_orchestrator import reference_status


def test_reference_identity_is_exact_filename_and_duplicate_does_not_relocate(
    tmp_path: Path,
) -> None:
    context = ProjectContext.create(tmp_path / "project", "Identity")
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
    context = ProjectContext.create(tmp_path / "project", "Signatures")
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
    context = ProjectContext.create(tmp_path / "project", "Relocate")
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
    context = ProjectContext.create(tmp_path / "project", "Recovery")
    reference_path = tmp_path / "reference.txt"
    reference_path.write_text("reference", encoding="utf-8")
    reference = register_reference_asset(context, reference_path)
    source_run = context.registry.create_source_run(
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
        assert context.registry.run(source_run)["kind"] == "source"
        assert context.registry.run(reference_run)["kind"] == "reference"
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
