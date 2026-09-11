from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from cueflow.artifact_versions import ARTIFACT_PRODUCER_VERSIONS
from cueflow.schema import ARTIFACT_KINDS


def test_every_artifact_kind_has_exactly_one_producer_version() -> None:
    assert set(ARTIFACT_PRODUCER_VERSIONS) == ARTIFACT_KINDS
    assert len(ARTIFACT_PRODUCER_VERSIONS) == 18


def test_v054_artifact_golden_is_reproducible_in_two_clean_processes(tmp_path: Path) -> None:
    runner = Path(__file__).with_name("_artifact_golden_harness.py")
    first, second = tmp_path / "first.json", tmp_path / "second.json"
    for output in (first, second):
        subprocess.run([sys.executable, str(runner), str(output)], check=True)
    golden = Path(__file__).parent / "fixtures" / "v054_artifact_id_golden.json"
    expected = json.loads(golden.read_text(encoding="utf-8"))
    assert expected["baseline_commit"] == "53d56c9"
    assert expected["source_manifest_sha256"] == (
        "6000CA674908A5BEB460FC3C16069C76FE9E32CBBD820761F8A22A3BB68A4805"
    )
    assert all(
        set(artifact)
        == {"artifact_kind", "scope_key", "artifact_id", "content_hash", "producer"}
        for artifact in expected["artifacts"]
    )
    assert json.loads(first.read_text(encoding="utf-8")) == expected
    assert json.loads(second.read_text(encoding="utf-8")) == expected
