from __future__ import annotations

import json
from pathlib import Path

import pytest

from cueflow.cli import build_parser, main


def test_reference_cli_add_extract_status_and_relocate(
    tmp_path: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    project = tmp_path / "project"
    reference = tmp_path / "notes.txt"
    reference.write_text("CLI reference", encoding="utf-8")
    assert main(
        [
            "init",
            str(project),
            "--name",
            "CLI",
            "--profile",
            "LOCAL_PROFILE",
        ]
    ) == 0
    capfd.readouterr()

    assert main(["reference", "add", str(project), str(reference)]) == 0
    added = json.loads(capfd.readouterr().out)
    reference_asset_id = added["reference_asset_id"]
    assert added["status"] == "registered"

    assert main(["reference", "extract", str(project), reference_asset_id]) == 0
    extracted = json.loads(capfd.readouterr().out)
    assert extracted["outcome"] == "complete"

    assert main(["reference", "status", str(project), reference_asset_id]) == 0
    status = json.loads(capfd.readouterr().out)
    assert status["reference_assets"][0]["runs"][0]["run_id"] == extracted["run_id"]

    assert main(["reference", "relocate", str(project), str(tmp_path)]) == 0
    relocated = json.loads(capfd.readouterr().out)
    assert relocated["relocated"] == []
    assert relocated["skipped_readable_reference_ids"] == [reference_asset_id]


def test_reference_cli_surface_has_no_rejected_switches() -> None:
    help_text = build_parser().format_help()
    assert "--document-visual" not in help_text
    assert "--audio-upload-format" not in help_text
    with pytest.raises(SystemExit):
        build_parser().parse_args(
            ["reference", "extract", "project", "ref_id", "--document-visual", "all"]
        )
    with pytest.raises(SystemExit):
        build_parser().parse_args(
            [
                "reference",
                "extract",
                "project",
                "ref_id",
                "--audio-upload-format",
                "opus",
            ]
        )
