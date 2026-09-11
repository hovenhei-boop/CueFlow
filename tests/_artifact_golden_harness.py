from __future__ import annotations

import json
import sys
import tempfile
import uuid
import wave
from pathlib import Path
from typing import Any

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "tests"))

from test_orchestrator_v052 import FakeMediaStore  # noqa: E402
from test_post_correction_pipeline import (  # noqa: E402
    LongAsr,
    LongCorrection,
    LongKimi,
    TextAta,
    WindowGlm,
    _one_case_batches,
)

from cueflow.api import Workspace  # noqa: E402
from cueflow.config import RuntimeConfig  # noqa: E402
from cueflow.media import ProbeResult  # noqa: E402
from cueflow.orchestrator import resolve_review, run_project  # noqa: E402
from cueflow.schema import ARTIFACT_KINDS  # noqa: E402

SOURCE_MANIFEST_SHA256 = (
    "6000CA674908A5BEB460FC3C16069C76FE9E32CBBD820761F8A22A3BB68A4805"
)


class DeterministicUuid:
    def __init__(self) -> None:
        self.value = 0

    def __call__(self) -> uuid.UUID:
        self.value += 1
        return uuid.UUID(int=self.value)


def _patch_determinism(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(uuid, "uuid4", DeterministicUuid())

    def fixed_now() -> str:
        return "2026-09-11T00:00:00Z"
    for module_name in (
        "cueflow.schema",
        "cueflow.registry",
        "cueflow.project",
        "cueflow.lifecycle",
        "cueflow.object_storage",
    ):
        module = sys.modules.get(module_name)
        if module is not None and hasattr(module, "utc_now"):
            monkeypatch.setattr(module, "utc_now", fixed_now)


def _workspace(root: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Any, Workspace]:
    import cueflow.media as media_module
    import cueflow.orchestrator as orchestrator

    source = root / "source.wav"
    source.write_bytes(b"fixed-source")
    workspace = Workspace(root / "workspace")
    context = workspace.context(workspace.registry.create_run())
    duration_ms = 100_000

    def fake_probe(_path: Path, _runtime: RuntimeConfig) -> ProbeResult:
        return ProbeResult(
            "audio",
            duration_ms,
            duration_ms * 16,
            {
                "timeline_status": "normal",
                "presentation_duration_ms": duration_ms,
                "presentation_total_samples": duration_ms * 16,
            },
        )

    def fake_render(
        _source: Path, _probe: ProbeResult, destination: Path, _runtime: RuntimeConfig
    ) -> None:
        with wave.open(str(destination), "wb") as wav_file:
            wav_file.setnchannels(1)
            wav_file.setsampwidth(2)
            wav_file.setframerate(16_000)
            wav_file.writeframes(b"\0\0" * duration_ms * 16)

    monkeypatch.setattr(orchestrator, "probe_source", fake_probe)
    monkeypatch.setattr(media_module, "render_timeline_audio", fake_render)
    return source, context, workspace


def build_manifest(output: Path) -> None:
    monkeypatch = pytest.MonkeyPatch()
    temporary = tempfile.TemporaryDirectory(prefix="cueflow-v060-golden-")
    root = Path(temporary.name)
    _patch_determinism(monkeypatch)
    _one_case_batches(monkeypatch)
    WindowGlm.calls, WindowGlm.failures, WindowGlm.next_window = [], {1}, [0, 1, 2]
    LongCorrection.calls, TextAta.texts = 0, []
    source, context, workspace = _workspace(root, monkeypatch)
    try:
        pending = run_project(
            context,
            source,
            keywords=["NVIDIA"],
            runtime=RuntimeConfig("ffmpeg", "ffprobe"),
            media_store_factory=FakeMediaStore,
            qwen_asr_factory=LongAsr,
            doubao_asr_factory=LongAsr,
            qwen_correction_factory=LongCorrection,
            kimi_correction_factory=LongKimi,
            glm_selection_factory=WindowGlm,
            ata_factory=TextAta,
        )
        if pending["status"] != "needs_review":
            raise AssertionError(pending)
        queue = context.current_artifact("review_queue")
        completed = resolve_review(
            context,
            [{"review_id": queue.payload["items"][0]["review_id"], "action": "keep"}],
            run_id=pending["run_id"],
            expected_review_queue_artifact_id=queue.artifact_id,
            media_store_factory=FakeMediaStore,
            ata_factory=TextAta,
        )
        if completed["status"] != "succeeded":
            raise AssertionError(completed)
        rows = context.registry.connection.execute(
            "SELECT artifact_id, artifact_kind, scope_key FROM artifacts "
            "ORDER BY artifact_kind, scope_key, artifact_id"
        ).fetchall()
        artifacts = []
        for row in rows:
            envelope = context.artifact(str(row["artifact_id"]))
            artifacts.append(
                {
                    "artifact_kind": envelope.artifact_kind,
                    "scope_key": envelope.scope_key,
                    "artifact_id": envelope.artifact_id,
                    "content_hash": envelope.content_hash,
                    "producer": envelope.producer.as_dict(),
                }
            )
        if {str(item["artifact_kind"]) for item in artifacts} != ARTIFACT_KINDS:
            raise AssertionError("golden scenario does not cover every Artifact kind")
        manifest = {
            "baseline_commit": "53d56c9",
            "source_manifest_sha256": SOURCE_MANIFEST_SHA256,
            "purpose": (
                "Fake object-store golden proving v0.5.4 to v0.6.0 producer-version "
                "refactor equivalence; not a production cross-Run Artifact ID guarantee."
            ),
            "artifacts": artifacts,
        }
        output.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    finally:
        context.close()
        workspace.close()
        monkeypatch.undo()
        temporary.cleanup()


if __name__ == "__main__":
    build_manifest(Path(sys.argv[1]))
