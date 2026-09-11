from pathlib import Path

import pytest
from test_orchestrator_v052 import FakeMediaStore

from cueflow.errors import ContractError, UnsupportedReferenceError
from cueflow.job_inputs import ReferenceSpec
from cueflow.project import RunContext
from cueflow.reference_preparation import capture_references, prepare_references


def test_reference_order_types_and_text_snapshot(tmp_path: Path):
    context = RunContext.create(tmp_path / "run", "fixture")
    notes = tmp_path / "notes.md"
    notes.write_text("Blackwell notes", encoding="utf-8")
    pdf = tmp_path / "a.pdf"
    pdf.write_bytes(b"%PDF-1.7\n1 0 obj<</Type/Catalog>>endobj\n%%EOF\n")
    try:
        capture_references(
            context, [ReferenceSpec("file", str(pdf)), ReferenceSpec("file", str(notes))]
        )
        notes.write_text("changed", encoding="utf-8")
        references = prepare_references(context, FakeMediaStore)
        assert [item["kind"] for item in references] == ["pdf_object", "text"]
        assert references[1]["text"] == "Blackwell notes"
        assert "url" not in references[0]
        assert references[0]["object"]["object_key"]
    finally:
        context.close()


@pytest.mark.parametrize("suffix", ["doc", "docx", "ppt", "pptx", "xls", "xlsx"])
def test_office_failure_is_unavailable_without_failing_run(tmp_path: Path, suffix: str):
    context = RunContext.create(tmp_path / "run", "fixture")
    value = tmp_path / f"input.{suffix}"
    value.write_bytes(b"office")

    def unavailable(source, directory):
        raise UnsupportedReferenceError("converter unavailable")

    try:
        capture_references(context, [ReferenceSpec("file", str(value))])
        assert prepare_references(context, FakeMediaStore, converter=unavailable) == []
        row = context.registry.connection.execute("SELECT * FROM reference_preparations").fetchone()
        assert row["status"] == "unavailable"
        assert context.registry.run(context.run_id)["status"] == "queued"
    finally:
        context.close()


def test_caller_provided_urls_are_rejected(tmp_path: Path):
    context = RunContext.create(tmp_path / "run", "fixture")
    try:
        with pytest.raises(ContractError, match="caller-provided URLs"):
            capture_references(context, [ReferenceSpec("pdf_url", "https://example.com/a.pdf")])
    finally:
        context.close()
