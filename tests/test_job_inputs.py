from __future__ import annotations

from pathlib import Path

import pytest

from cueflow.errors import UnsupportedReferenceError
from cueflow.job_inputs import ReferenceSpec, build_job_input_payload


def test_reference_order_types_and_text_snapshot(tmp_path: Path) -> None:
    notes = tmp_path / "notes.md"
    notes.write_text("Blackwell notes", encoding="utf-8")
    payload = build_job_input_payload(
        source_asset_id="src_1",
        references=[
            ReferenceSpec("image_url", "https://example.com/download?id=123"),
            ReferenceSpec("text_file", str(notes)),
            ReferenceSpec("pdf_url", "https://example.com/report"),
        ],
        keywords=[" Blackwell, ", "Blackwell", " NVIDIA "],
    )
    references = payload["references"]
    assert isinstance(references, list)
    assert [item["kind"] for item in references] == ["image_url", "text", "pdf_url"]
    assert references[0]["locator_semantics"] == "mutable_remote_locator"
    assert references[1]["text"] == "Blackwell notes"
    notes.write_text("changed", encoding="utf-8")
    assert references[1]["text"] == "Blackwell notes"
    assert payload["user_keywords"] == ["Blackwell,", "Blackwell", "NVIDIA"]


@pytest.mark.parametrize("suffix", ["doc", "docx", "ppt", "pptx", "xls", "xlsx"])
def test_office_is_explicitly_unsupported(tmp_path: Path, suffix: str) -> None:
    value = tmp_path / f"input.{suffix}"
    value.write_bytes(b"office")
    with pytest.raises(UnsupportedReferenceError, match="export the file to PDF"):
        build_job_input_payload(
            source_asset_id="src_1",
            references=[ReferenceSpec("text_file", str(value))],
        )


def test_remote_references_require_https() -> None:
    with pytest.raises(UnsupportedReferenceError, match="HTTPS"):
        build_job_input_payload(
            source_asset_id="src_1",
            references=[ReferenceSpec("pdf_url", "http://example.com/a.pdf")],
        )
