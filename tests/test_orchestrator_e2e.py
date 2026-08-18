from __future__ import annotations

import hashlib
import os
import subprocess
import wave
from pathlib import Path

import pytest

import cueflow.orchestrator as orchestrator_module
from cueflow.atomizer import atomize
from cueflow.config import RuntimeConfig, RuntimeDeviceConfig
from cueflow.errors import DeliveryAmbiguousError, ExportBlockedError
from cueflow.media import ProbeResult, _packet_discontinuity
from cueflow.orchestrator import initialize_project, project_status, run_project
from cueflow.providers import AlignmentToken, SemanticResult


class FakeSemantic:
    provider = "test-semantic"
    model = "test-verbatim"
    revision = "fixture"

    def transcribe(
        self,
        audio_path: Path,
        glossary_terms: list[str],
        *,
        rework_context: str | None = None,
    ) -> SemanticResult:
        assert audio_path.is_file()
        return SemanticResult(source_text="这个方案可以啊。", language="Chinese")

    def close(self) -> None:
        return None


class FakeAligner:
    provider = "qwen-local-fixture"
    model = "Qwen3-ForcedAligner-0.6B-fixture"
    revision = "fixture"

    def align(self, audio_path: Path, text: str, language: str | None) -> list[AlignmentToken]:
        assert audio_path.is_file()
        _, atoms = atomize(text)
        return [
            AlignmentToken(str(atom["text"]), index * 150, (index + 1) * 150)
            for index, atom in enumerate(atoms)
        ]

    def close(self) -> None:
        return None


class AmbiguousSemantic(FakeSemantic):
    calls = 0

    def transcribe(
        self,
        audio_path: Path,
        glossary_terms: list[str],
        *,
        rework_context: str | None = None,
    ) -> SemanticResult:
        type(self).calls += 1
        raise DeliveryAmbiguousError("fixture delivery state is ambiguous")


class UnalignableAligner(FakeAligner):
    calls = 0

    def align(self, audio_path: Path, text: str, language: str | None) -> list[AlignmentToken]:
        type(self).calls += 1
        return [AlignmentToken("错", 0, 100)]


class StableConflictSemantic(FakeSemantic):
    calls = 0

    def transcribe(
        self,
        audio_path: Path,
        glossary_terms: list[str],
        *,
        rework_context: str | None = None,
    ) -> SemanticResult:
        type(self).calls += 1
        assert glossary_terms == ["顾华玺"]
        if type(self).calls > 1:
            assert rework_context is not None and "顾华玺" in rework_context
        return SemanticResult(source_text="顾华西老师", language="Chinese")


def _runtime() -> RuntimeConfig:
    ffmpeg = os.getenv("CUEFLOW_FFMPEG")
    ffprobe = os.getenv("CUEFLOW_FFPROBE")
    if not ffmpeg or not ffprobe:
        pytest.skip("real media E2E requires CUEFLOW_FFMPEG and CUEFLOW_FFPROBE")
    return RuntimeConfig(
        ffmpeg=ffmpeg,
        ffprobe=ffprobe,
        model_cache=None,
        device=RuntimeDeviceConfig(device="cpu", dtype="float32"),
    )


def _video(path: Path, runtime: RuntimeConfig) -> str:
    subprocess.run(
        [
            runtime.ffmpeg,
            "-v",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=blue:s=1280x720:r=30:d=4",
            "-itsoffset",
            "0.5",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=48000:duration=3",
            "-map",
            "0:v:0",
            "-map",
            "1:a:0",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-t",
            "4",
            str(path),
        ],
        check=True,
    )
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_full_orchestrator_preserves_source_and_exports_only_srt(tmp_path: Path) -> None:
    runtime = _runtime()
    source = tmp_path / "external-source.mp4"
    source_hash = _video(source, runtime)
    context = initialize_project(tmp_path / "project", "E2E", "LOCAL_PROFILE")
    try:
        (context.root / "output" / "subtitles.srt").write_text("old", encoding="utf-8")
        result = run_project(
            context,
            source,
            runtime=runtime,
            semantic_factory=lambda profile, value: FakeSemantic(),
            aligner_factory=lambda value: FakeAligner(),
        )
        assert result["status"] == "succeeded"
        assert hashlib.sha256(source.read_bytes()).hexdigest() == source_hash
        assert context.registry.source_asset(
            context.project_id, str(result["source_asset_id"])
        )["storage_mode"] == "external_reference"
        proxy = context.current_artifact("video_proxy")
        assert proxy.payload["width"] == 640
        assert proxy.payload["height"] == 360
        probe = context.current_artifact("media_probe")
        assert probe.payload["timeline_status"] == "corrected"
        assert probe.payload["timeline_actions"][0]["action"] == "pad_silence_before"
        timeline = context.current_artifact("timeline_audio")
        assert timeline.payload["sample_rate_hz"] == 16_000
        assert timeline.payload["channels"] == 1
        assert timeline.payload["sample_format"] == "s16le"
        timeline_path = context.store.blob_path(timeline.payload["audio_blob"]["content_hash"])
        with wave.open(str(timeline_path), "rb") as timeline_wave:
            assert timeline_wave.getframerate() == 16_000
            assert timeline_wave.getnchannels() == 1
            assert timeline_wave.getsampwidth() == 2
            assert abs(timeline_wave.getnframes() / 16 - 4_000) <= 20
        assert not context.store.blob_path("sha256:" + source_hash).exists()
        output_files = sorted(
            path.name for path in (context.root / "output").iterdir() if path.is_file()
        )
        assert output_files == ["subtitles.srt"]
        srt = (context.root / "output" / "subtitles.srt").read_text(encoding="utf-8")
        assert "这个方案可以" in srt
        assert "这个方案可以啊" not in srt
        assert "00:00:00,000 --> 00:00:01,050" in srt
        assert not list((context.root / "output").glob("*.tmp"))
        assert context.registry.current_pointers(context.project_id, "alignment")
        assert not any(
            row["scope_key"] == "global"
            for row in context.registry.current_pointers(context.project_id, "alignment")
        )
        assert project_status(context)["latest_run"]["status"] == "succeeded"
    finally:
        context.close()


def test_unaligned_atoms_get_one_repair_then_block_export(tmp_path: Path) -> None:
    runtime = _runtime()
    source = tmp_path / "external-source.mp4"
    _video(source, runtime)
    context = initialize_project(tmp_path / "project", "Blocked", "LOCAL_PROFILE")
    UnalignableAligner.calls = 0
    try:
        with pytest.raises(ExportBlockedError, match="one repair pass"):
            run_project(
                context,
                source,
                runtime=runtime,
                semantic_factory=lambda profile, value: FakeSemantic(),
                aligner_factory=lambda value: UnalignableAligner(),
            )
        assert UnalignableAligner.calls == 2
        assert not (context.root / "output" / "subtitles.srt").exists()
        assert project_status(context)["latest_run"]["status"] == "failed"
    finally:
        context.close()


def test_glossary_rework_publishes_new_versions_and_stable_warning(tmp_path: Path) -> None:
    runtime = _runtime()
    source = tmp_path / "external-source.mp4"
    _video(source, runtime)
    context = initialize_project(tmp_path / "project", "Rework", "LOCAL_PROFILE")
    set_glossary = orchestrator_module.set_project_glossary
    set_glossary(context, ["顾华玺"])
    StableConflictSemantic.calls = 0
    try:
        result = run_project(
            context,
            source,
            runtime=runtime,
            semantic_factory=lambda profile, value: StableConflictSemantic(),
            aligner_factory=lambda value: FakeAligner(),
        )
        assert result["status"] == "succeeded"
        assert StableConflictSemantic.calls == 2
        latest = context.registry.latest_run(context.project_id)
        assert latest is not None
        invocations = context.registry.invocations_for_run(str(latest["run_id"]))
        semantic_invocations = [
            row for row in invocations if row["operation"] == "semantic_transcription"
        ]
        assert len(semantic_invocations) == 2
        assert semantic_invocations[0]["artifact_id"] != semantic_invocations[1]["artifact_id"]
        qa = context.current_artifact("qa")
        issue = next(
            item for item in qa.payload["issues"] if item["code"] == "stable_glossary_conflict"
        )
        assert issue["replacement_artifact_ids"] == [semantic_invocations[1]["artifact_id"]]
    finally:
        context.close()


def test_timestamp_discontinuity_is_unverified_warning_and_does_not_block_srt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    packets = [
        {"stream_index": 1, "pts_time": "0.000", "duration_time": "0.020"},
        {"stream_index": 1, "pts_time": "0.100", "duration_time": "0.020"},
    ]
    assert _packet_discontinuity(packets, 1, 20) == "audio_timestamp_discontinuity"
    runtime = _runtime()
    source = tmp_path / "external-source.mp4"
    _video(source, runtime)
    real_probe = orchestrator_module.probe_source

    def discontinuous_probe(path: Path, value: RuntimeConfig) -> ProbeResult:
        probed = real_probe(path, value)
        payload = dict(probed.payload)
        payload["timeline_status"] = "unverified"
        payload["timeline_issues"] = [
            *payload["timeline_issues"],
            "audio_timestamp_discontinuity",
        ]
        return ProbeResult(probed.media_kind, probed.duration_ms, payload)

    monkeypatch.setattr(orchestrator_module, "probe_source", discontinuous_probe)
    context = initialize_project(tmp_path / "project", "Unverified", "LOCAL_PROFILE")
    try:
        result = run_project(
            context,
            source,
            runtime=runtime,
            semantic_factory=lambda profile, value: FakeSemantic(),
            aligner_factory=lambda value: FakeAligner(),
        )
        assert result["status"] == "succeeded"
        assert {item["code"] for item in result["warnings"]} >= {
            "timeline_status_unverified"
        }
        assert (context.root / "output" / "subtitles.srt").is_file()
        assert {item["code"] for item in project_status(context)["warnings"]} >= {
            "timeline_status_unverified"
        }
    finally:
        context.close()


def test_delivery_ambiguous_semantic_invocation_is_not_automatically_retried(
    tmp_path: Path,
) -> None:
    runtime = _runtime()
    source = tmp_path / "external-source.mp4"
    _video(source, runtime)
    context = initialize_project(tmp_path / "project", "Ambiguous", "LOCAL_PROFILE")
    AmbiguousSemantic.calls = 0
    try:
        with pytest.raises(DeliveryAmbiguousError):
            run_project(
                context,
                source,
                runtime=runtime,
                semantic_factory=lambda profile, value: AmbiguousSemantic(),
                aligner_factory=lambda value: FakeAligner(),
            )
        assert AmbiguousSemantic.calls == 1
        latest = context.registry.latest_run(context.project_id)
        assert latest is not None
        invocations = context.registry.invocations_for_run(str(latest["run_id"]))
        assert [row["status"] for row in invocations] == ["delivery_ambiguous"]
    finally:
        context.close()
