from __future__ import annotations

import json
import os
import subprocess
import wave
from pathlib import Path

import pytest

import cueflow.cli as cli_module
import cueflow.orchestrator as orchestrator_module
from cueflow.atomizer import atomize
from cueflow.config import RuntimeConfig, RuntimeDeviceConfig
from cueflow.errors import DeliveryAmbiguousError, ExportBlockedError, SourceMissingError
from cueflow.media import ProbeResult
from cueflow.orchestrator import initialize_project, project_status, run_project
from cueflow.providers import AlignmentToken, SemanticResult
from cueflow.schema import validate_payload


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
        assert glossary_terms == ["秦明", "顾华玺"]
        if type(self).calls > 1:
            assert rework_context is not None
        values = (
            "顾华西老师，秦明",
            "顾华玺老师，秦民",
            "顾华玺老师，秦民",
        )
        return SemanticResult(source_text=values[type(self).calls - 1], language="Chinese")


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


def _video(path: Path, runtime: RuntimeConfig) -> None:
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


def test_full_orchestrator_preserves_source_and_exports_only_srt(tmp_path: Path) -> None:
    runtime = _runtime()
    source = tmp_path / "external-source.mp4"
    _video(source, runtime)
    source_bytes = source.read_bytes()
    context = initialize_project(tmp_path / "project", "E2E")
    try:
        (context.root / "output" / "subtitles.srt").write_text("old", encoding="utf-8")
        result = run_project(
            context,
            source,
            runtime=runtime,
            semantic_factory=lambda: FakeSemantic(),
            aligner_factory=lambda value: FakeAligner(),
        )
        assert result["status"] == "succeeded"
        assert source.read_bytes() == source_bytes
        assert context.registry.source_asset(
            context.project_id, str(result["source_asset_id"])
        )["storage_mode"] == "external_reference"
        probe = context.current_artifact("media_probe")
        validate_payload("media_probe", probe.payload)
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
            assert timeline_wave.getnframes() == timeline.payload["total_sample_count"]
        assert all(
            path.read_bytes() != source_bytes
            for path in context.store.blobs_root.rglob("*")
            if path.is_file()
        )
        output_files = sorted(
            path.name for path in (context.root / "output").iterdir() if path.is_file()
        )
        assert output_files == ["subtitles.srt"]
        srt = (context.root / "output" / "subtitles.srt").read_text(encoding="utf-8")
        assert "这个方案可以啊" in srt
        assert "00:00:00,000 --> 00:00:01,050" in srt
        assert not list((context.root / "output").glob("*.tmp"))
        assert context.registry.current_pointers(context.project_id, "alignment")
        assert not any(
            row["scope_key"] == "global"
            for row in context.registry.current_pointers(context.project_id, "alignment")
        )
        assert project_status(context)["latest_source_run"]["status"] == "succeeded"
    finally:
        context.close()


def test_unaligned_atoms_get_one_repair_then_block_export(tmp_path: Path) -> None:
    runtime = _runtime()
    source = tmp_path / "external-source.mp4"
    _video(source, runtime)
    context = initialize_project(tmp_path / "project", "Blocked")
    UnalignableAligner.calls = 0
    try:
        with pytest.raises(ExportBlockedError, match="allowed structural repair"):
            run_project(
                context,
                source,
                runtime=runtime,
                semantic_factory=lambda: FakeSemantic(),
                aligner_factory=lambda value: UnalignableAligner(),
            )
        assert UnalignableAligner.calls == 2
        assert not (context.root / "output" / "subtitles.srt").exists()
        assert project_status(context)["latest_source_run"]["status"] == "failed"
    finally:
        context.close()


def test_glossary_rework_publishes_new_versions_and_stable_warning(tmp_path: Path) -> None:
    runtime = _runtime()
    source = tmp_path / "external-source.mp4"
    _video(source, runtime)
    context = initialize_project(tmp_path / "project", "Rework")
    set_glossary = orchestrator_module.set_project_glossary
    set_glossary(context, ["顾华玺", "秦明"])
    StableConflictSemantic.calls = 0
    try:
        result = run_project(
            context,
            source,
            runtime=runtime,
            semantic_factory=lambda: StableConflictSemantic(),
            aligner_factory=lambda value: FakeAligner(),
        )
        assert result["status"] == "succeeded"
        assert StableConflictSemantic.calls == 3
        latest = context.registry.latest_run(context.project_id)
        assert latest is not None
        invocations = context.registry.invocations_for_run(str(latest["run_id"]))
        semantic_invocations = [
            row for row in invocations if row["operation"] == "semantic_transcription"
        ]
        assert len(semantic_invocations) == 3
        assert len({row["artifact_id"] for row in semantic_invocations}) == 3
        qa = context.current_artifact("qa")
        issue = next(
            item
            for item in qa.payload["issues"]
            if item["code"] == "stable_glossary_conflict"
            and item["observed"]["term"] == "秦明"
        )
        assert issue["replacement_artifact_ids"] == [
            semantic_invocations[1]["artifact_id"],
            semantic_invocations[2]["artifact_id"],
        ]
        assert any(
            item["code"] == "glossary_single_atom_conflict"
            and item["resolution_status"] == "resolved"
            and item["observed"]["term"] == "顾华玺"
            for item in qa.payload["issues"]
        )
    finally:
        context.close()


def test_timestamp_discontinuity_is_unverified_warning_and_does_not_block_srt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
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
        return ProbeResult(
            probed.media_kind,
            probed.duration_ms,
            probed.total_sample_count,
            payload,
        )

    monkeypatch.setattr(orchestrator_module, "probe_source", discontinuous_probe)
    context = initialize_project(tmp_path / "project", "Unverified")
    try:
        result = run_project(
            context,
            source,
            runtime=runtime,
            semantic_factory=lambda: FakeSemantic(),
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


def test_source_missing_fails_without_relink_or_guessing(tmp_path: Path) -> None:
    context = initialize_project(tmp_path / "project", "Missing")
    try:
        with pytest.raises(SourceMissingError, match="source_missing"):
            run_project(context, tmp_path / "moved.mp4", runtime=_runtime())
    finally:
        context.close()


def test_delivery_ambiguous_semantic_invocation_is_not_automatically_retried(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    runtime = _runtime()
    source = tmp_path / "external-source.mp4"
    _video(source, runtime)
    context = initialize_project(tmp_path / "project", "Ambiguous")
    AmbiguousSemantic.calls = 0
    try:
        with pytest.raises(DeliveryAmbiguousError):
            run_project(
                context,
                source,
                runtime=runtime,
                semantic_factory=lambda: AmbiguousSemantic(),
                aligner_factory=lambda value: FakeAligner(),
            )
        assert AmbiguousSemantic.calls == 1
        latest = context.registry.latest_run(context.project_id)
        assert latest is not None
        run_id = str(latest["run_id"])
        invocations = context.registry.invocations_for_run(str(latest["run_id"]))
        assert [row["status"] for row in invocations] == ["delivery_ambiguous"]
        invocation_id = str(invocations[0]["invocation_id"])
        assert context.registry.run(run_id)["status"] == "failed"

        def ambiguous_retry(context_value: object, target_id: str) -> dict[str, object]:
            assert target_id == invocation_id
            raise DeliveryAmbiguousError("still ambiguous")

        monkeypatch.setattr(cli_module, "retry_invocation", ambiguous_retry)
        assert cli_module.main(["retry", str(context.root), invocation_id]) == 2
        failure = json.loads(capsys.readouterr().err)
        assert failure["run_id"] == run_id
        assert failure["invocation_id"] == invocation_id
        assert failure["invocation_status"] == "delivery_ambiguous"
        assert failure["next_actions"][0] == {
            "action": "retry",
            "invocation_id": invocation_id,
            "requires_explicit_user_action": True,
            "automatic_retry": False,
        }
        assert "will not retry automatically" in failure["delivery_warning"]

        def interrupted_retry(context_value: object, target_id: str) -> dict[str, object]:
            assert target_id == invocation_id
            raise KeyboardInterrupt

        monkeypatch.setattr(cli_module, "retry_invocation", interrupted_retry)
        assert cli_module.main(["retry", str(context.root), invocation_id]) == 130
        interrupted_failure = json.loads(capsys.readouterr().err)
        assert interrupted_failure["status"] == "failed"
        assert interrupted_failure["run_id"] == run_id
        assert interrupted_failure["invocation_status"] == "delivery_ambiguous"
    finally:
        context.close()

    class InterruptedSemantic(FakeSemantic):
        def transcribe(
            self,
            audio_path: Path,
            glossary_terms: list[str],
            *,
            rework_context: str | None = None,
        ) -> SemanticResult:
            raise KeyboardInterrupt

    class CrashedSemantic(FakeSemantic):
        def transcribe(
            self,
            audio_path: Path,
            glossary_terms: list[str],
            *,
            rework_context: str | None = None,
        ) -> SemanticResult:
            raise RuntimeError("unexpected provider crash")

    for name, provider_type, error_type, expected_run_status in (
        ("ctrl-c", InterruptedSemantic, KeyboardInterrupt, "interrupted"),
        ("crash", CrashedSemantic, RuntimeError, "failed"),
    ):
        interrupted_context = initialize_project(
            tmp_path / name, name
        )
        try:
            with pytest.raises(error_type):
                run_project(
                    interrupted_context,
                    source,
                    runtime=runtime,
                    semantic_factory=lambda cls=provider_type: cls(),
                    aligner_factory=lambda value: FakeAligner(),
                )
            interrupted_run = interrupted_context.registry.latest_run(
                interrupted_context.project_id
            )
            assert interrupted_run is not None
            assert interrupted_run["status"] == expected_run_status
            interrupted_invocations = interrupted_context.registry.invocations_for_run(
                str(interrupted_run["run_id"])
            )
            assert [row["status"] for row in interrupted_invocations] == [
                "delivery_ambiguous"
            ]
        finally:
            interrupted_context.close()

    created_context = initialize_project(
        tmp_path / "created-ctrl-c", "created-ctrl-c"
    )
    real_set_invocation_status = created_context.registry.set_invocation_status

    def interrupt_before_sending(
        target_id: str, status: str, **kwargs: object
    ) -> None:
        if status == "sending":
            raise KeyboardInterrupt
        real_set_invocation_status(target_id, status, **kwargs)

    monkeypatch.setattr(
        created_context.registry, "set_invocation_status", interrupt_before_sending
    )
    try:
        with pytest.raises(KeyboardInterrupt):
            run_project(
                created_context,
                source,
                runtime=runtime,
                semantic_factory=lambda: FakeSemantic(),
                aligner_factory=lambda value: FakeAligner(),
            )
        created_run = created_context.registry.latest_run(created_context.project_id)
        assert created_run is not None and created_run["status"] == "interrupted"
        created_invocations = created_context.registry.invocations_for_run(
            str(created_run["run_id"])
        )
        assert [row["status"] for row in created_invocations] == [
            "definitely_not_sent"
        ]
        assert (
            created_context.registry.sent_semantic_attempt_count(
                str(created_run["run_id"]), "chunk_0001", 0
            )
            == 0
        )
    finally:
        created_context.close()
