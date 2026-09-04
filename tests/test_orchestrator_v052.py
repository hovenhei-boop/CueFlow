from __future__ import annotations

import wave
from pathlib import Path
from typing import Any

import pytest

from cueflow.asr_contracts import AsrResult, ProviderMetadata, TimedUnit
from cueflow.ata_provider import AlignmentResult
from cueflow.canonical import hash_json
from cueflow.config import RuntimeConfig
from cueflow.correction_provider import CorrectionRequest, CorrectionResult
from cueflow.edit_resolution import Edit
from cueflow.errors import ProviderError
from cueflow.media import MediaBundle, ProbeResult
from cueflow.media_object_store import MediaObjectRef
from cueflow.orchestrator import initialize_project, resolve_review, retry_invocation, run_project
from cueflow.schema import ArtifactEnvelope, InputRef, Producer


class FakeMediaStore:
    provider = "fake-object-store"

    def upload(self, path: Path) -> MediaObjectRef:
        assert path.read_bytes() == b"source"
        return MediaObjectRef(self.provider, "bucket", "key", "sha256:" + "a" * 64, 6)

    def presign_get(self, ref: MediaObjectRef) -> str:
        assert ref.object_key == "key"
        return "https://media.example/source.wav?signature=private"

    def close(self) -> None:
        pass


class FakeWholeAsr:
    calls: list[tuple[str, tuple[str, ...]]] = []
    provider = "fake"
    model = "fake-model"
    text = ""

    def transcribe(self, media_url: str, *, user_keywords: Any) -> AsrResult:
        type(self).calls.append((media_url, tuple(user_keywords)))
        return AsrResult(
            self.text,
            (TimedUnit(self.text, 0, 2_000),),
            ProviderMetadata(self.provider, self.model, self.model, "response", 10),
        )

    def close(self) -> None:
        pass


class FakeQwenAsr(FakeWholeAsr):
    provider = "fake-qwen"
    text = "Qwen38 is good."


class FakeDoubaoAsr(FakeWholeAsr):
    provider = "fake-doubao"
    text = "Qwen3.8 is good."


class FakeGlm:
    provider = "fake-glm"
    model = "glm-asr-2512"
    keywords: tuple[str, ...] = ()

    def transcribe(self, audio_path: Path, *, user_keywords: Any) -> AsrResult:
        assert audio_path.stat().st_size > 44
        type(self).keywords = tuple(user_keywords)
        return AsrResult(
            "Qwen3.8",
            (),
            ProviderMetadata(self.provider, self.model, self.model, "glm", 5),
        )

    def close(self) -> None:
        pass


class FakeCorrection:
    requests: list[CorrectionRequest] = []
    arm = ""
    provider = "fake-correction"
    model = "fake-correction-model"

    def correct(self, request: CorrectionRequest) -> CorrectionResult:
        type(self).requests.append(request)
        return CorrectionResult(
            (Edit("Qwen38 is good.", "Qwen38", "Qwen3.8"),),
            ProviderMetadata(self.provider, self.model, self.model, self.arm, 20),
        )

    def close(self) -> None:
        pass


class FakeQwenCorrection(FakeCorrection):
    arm = "qwen"


class FakeKimiCorrection(FakeCorrection):
    arm = "kimi"


class FakeAta:
    provider = "fake-ata"
    model = "ata"

    def align(self, media_url: str, transcript_text: str) -> AlignmentResult:
        assert media_url.startswith("https://media.example/")
        assert transcript_text == "Qwen3.8 is good."
        return AlignmentResult(
            (
                TimedUnit("Qwen3.8", 0, 700),
                TimedUnit("is", 800, 1_100),
                TimedUnit("good", 1_200, 1_800),
            ),
            (TimedUnit(transcript_text, 0, 1_800),),
            ProviderMetadata(self.provider, self.model, self.model, "ata", 8),
        )

    def close(self) -> None:
        pass


class FakeKimiNoEdit(FakeCorrection):
    arm = "kimi"

    def correct(self, request: CorrectionRequest) -> CorrectionResult:
        type(self).requests.append(request)
        return CorrectionResult(
            (), ProviderMetadata(self.provider, self.model, self.model, self.arm, 20)
        )


class FakeFailQwenCorrection(FakeCorrection):
    arm = "qwen"

    def correct(self, request: CorrectionRequest) -> CorrectionResult:
        type(self).requests.append(request)
        raise ProviderError("fixture correction failure")


def _project_with_fake_media(
    tmp_path: Path,
    monkeypatch: Any,
    duration_ms: int = 2_000,
) -> tuple[Path, Any]:
    import cueflow.orchestrator as orchestrator

    media_path = tmp_path / "source.wav"
    media_path.write_bytes(b"source")
    context = initialize_project(tmp_path / "project", "fixture")

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

    def fake_prepare(
        inner: Any, source: Any, probe: ProbeResult, _runtime: RuntimeConfig
    ) -> MediaBundle:
        producer = Producer("fixture", "0.5.2", None, None, hash_json({"fixture": True}))
        source_ref = InputRef(role="source_media", source_asset_id=str(source["source_asset_id"]))
        probe_artifact = ArtifactEnvelope.create(
            artifact_kind="media_probe",
            scope_key="global",
            producer=producer,
            inputs=[source_ref],
            payload=probe.payload,
        )
        wav_path = inner.store.temp_root / "timeline.wav"
        with wave.open(str(wav_path), "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(16_000)
            wav.writeframes(b"\0\0" * duration_ms * 16)
        blob_hash, length, _ = inner.store.publish_blob(wav_path)
        timeline = ArtifactEnvelope.create(
            artifact_kind="timeline_audio",
            scope_key="global",
            producer=producer,
            inputs=[
                source_ref,
                InputRef(role="media_probe", artifact_id=probe_artifact.artifact_id),
            ],
            payload={
                "source_asset_id": str(source["source_asset_id"]),
                "duration_ms": duration_ms,
                "total_sample_count": duration_ms * 16,
                "audio_blob": {
                    "content_hash": blob_hash,
                    "byte_length": length,
                    "media_type": "audio/wav",
                },
            },
        )
        for artifact in (probe_artifact, timeline):
            inner.publisher.publish(artifact)
        return MediaBundle(probe_artifact, timeline)

    monkeypatch.setattr(orchestrator, "probe_source", fake_probe)
    monkeypatch.setattr(orchestrator, "prepare_media", fake_prepare)
    return media_path, context


def test_complete_v052_pipeline_uses_same_user_keywords(tmp_path: Path, monkeypatch: Any) -> None:
    FakeQwenAsr.calls = []
    FakeDoubaoAsr.calls = []
    FakeQwenCorrection.requests = []
    FakeKimiCorrection.requests = []
    media_path, context = _project_with_fake_media(tmp_path, monkeypatch)
    try:
        result = run_project(
            context,
            media_path,
            keywords=[" .NET ", "GPT-5.6"],
            runtime=RuntimeConfig("ffmpeg", "ffprobe"),
            media_store_factory=FakeMediaStore,
            qwen_asr_factory=FakeQwenAsr,
            doubao_asr_factory=FakeDoubaoAsr,
            glm_asr_factory=lambda: (_ for _ in ()).throw(AssertionError("agreement called GLM")),
            qwen_correction_factory=FakeQwenCorrection,
            kimi_correction_factory=FakeKimiCorrection,
            ata_factory=FakeAta,
        )
        assert result["status"] == "succeeded"
        assert context.current_artifact("transcript").payload["source_text"] == "Qwen3.8 is good."
        assert FakeQwenAsr.calls[0][1] == (".NET", "GPT-5.6")
        assert FakeDoubaoAsr.calls[0][1] == (".NET", "GPT-5.6")
        assert FakeQwenCorrection.requests[0].peer_text == "Qwen3.8 is good."
        assert FakeQwenCorrection.requests[0].user_keywords == (".NET", "GPT-5.6")
        assert Path(result["output_path"]).is_file()
        assert not any(
            "signature=private" in str(item.payload)
            for item in (
                context.current_artifact("media_object"),
                context.current_artifact("base_asr"),
            )
        )
    finally:
        context.close()


def test_singleton_blocks_ata_until_review_is_resolved(tmp_path: Path, monkeypatch: Any) -> None:
    FakeQwenCorrection.requests = []
    FakeKimiNoEdit.requests = []
    media_path, context = _project_with_fake_media(tmp_path, monkeypatch)
    try:
        pending = run_project(
            context,
            media_path,
            keywords=["Qwen3.8"],
            runtime=RuntimeConfig("ffmpeg", "ffprobe"),
            media_store_factory=FakeMediaStore,
            qwen_asr_factory=FakeQwenAsr,
            doubao_asr_factory=FakeDoubaoAsr,
            glm_asr_factory=FakeGlm,
            qwen_correction_factory=FakeQwenCorrection,
            kimi_correction_factory=FakeKimiNoEdit,
            ata_factory=lambda: (_ for _ in ()).throw(AssertionError("ATA called before review")),
        )
        assert pending["status"] == "needs_review"
        assert not (context.root / "output" / "subtitles.srt").exists()

        result = resolve_review(
            context,
            [
                {
                    "review_id": context.current_artifact("review_queue").payload["items"][0][
                        "review_id"
                    ],
                    "action": "qwen",
                }
            ],
            run_id=pending["run_id"],
            expected_review_queue_artifact_id=pending["review_queue_artifact_id"],
            ata_factory=FakeAta,
            media_store_factory=FakeMediaStore,
        )
        assert result["status"] == "succeeded"
        assert context.current_artifact("transcript").payload["source_text"] == ("Qwen3.8 is good.")
    finally:
        context.close()


def test_explicit_correction_retry_preserves_identity_and_resumes_pipeline(
    tmp_path: Path, monkeypatch: Any
) -> None:
    media_path, context = _project_with_fake_media(tmp_path, monkeypatch)
    try:
        with pytest.raises(ProviderError, match="fixture"):
            run_project(
                context,
                media_path,
                keywords=["Qwen3.8"],
                runtime=RuntimeConfig("ffmpeg", "ffprobe"),
                media_store_factory=FakeMediaStore,
                qwen_asr_factory=FakeQwenAsr,
                doubao_asr_factory=FakeDoubaoAsr,
                glm_asr_factory=FakeGlm,
                qwen_correction_factory=FakeFailQwenCorrection,
                kimi_correction_factory=FakeKimiCorrection,
                ata_factory=FakeAta,
            )
        run_id = str(context.registry.runs(context.project_id)[-1]["run_id"])
        failed = context.registry.invocations_for_run(run_id)[-1]
        assert failed["operation"] == "qwen_correction"
        assert failed["status"] == "explicit_failure"

        result = retry_invocation(
            context,
            str(failed["invocation_id"]),
            media_store_factory=FakeMediaStore,
            qwen_correction_factory=FakeQwenCorrection,
            kimi_correction_factory=FakeKimiCorrection,
            ata_factory=FakeAta,
        )
        assert result["status"] == "succeeded"
        retries = context.registry.invocations_for_run(run_id)
        retry = next(row for row in retries if row["retry_of_invocation_id"])
        assert retry["retry_of_invocation_id"] == failed["invocation_id"]
        assert retry["idempotency_key"] == failed["idempotency_key"]
    finally:
        context.close()
