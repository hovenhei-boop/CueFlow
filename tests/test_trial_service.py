from __future__ import annotations

import hashlib
import wave
from pathlib import Path
from typing import Any

import pytest
from test_orchestrator_v052 import (
    FakeAta,
    FakeDoubaoAsr,
    FakeKimiCorrection,
    FakeMediaStore,
    FakeQwenAsr,
    FakeQwenCorrection,
)
from trial_helpers import initialized_store, trial_config

from cueflow.canonical import hash_json
from cueflow.config import RuntimeConfig
from cueflow.media import MediaBundle, ProbeResult
from cueflow.media_object_store import MediaObjectRef
from cueflow.orchestrator import ProviderFactories
from cueflow.schema import ArtifactEnvelope, InputRef, Producer
from cueflow.trial_pricing import TrialPricing
from cueflow.trial_service import TrialService
from cueflow.trial_storage import LifecycleRule
from cueflow.trial_store import TrialStore


class ReadyInspector:
    def check_bucket_access(self) -> None:
        return None

    def check_object_access(self, prefix: str, url_ttl_seconds: int) -> None:
        del prefix, url_ttl_seconds

    def rules(self) -> list[LifecycleRule]:
        return [
            LifecycleRule("trial/source", True, 7),
            LifecycleRule("trial/work", True, 7),
        ]


class SourceStore:
    provider = "fake-source"

    def upload(self, path: Path, *, object_name: str | None = None) -> MediaObjectRef:
        content = path.read_bytes()
        return MediaObjectRef(
            self.provider,
            "bucket",
            "trial/source/" + (object_name or path.name),
            "sha256:" + hashlib.sha256(content).hexdigest(),
            len(content),
        )

    def close(self) -> None:
        return None


class ResultStore(FakeMediaStore):
    provider = "fake-result-store"

    def plan_upload(self, path: Path, object_name: str) -> MediaObjectRef:
        ref = super().plan_upload(path, object_name)
        return MediaObjectRef(
            self.provider,
            ref.bucket,
            "trial/result/" + ref.object_key,
            ref.content_hash,
            ref.byte_length,
            ref.version_id,
            ref.mime_type,
        )


def test_successful_trial_persists_result_usage_snapshot_then_removes_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import cueflow.orchestrator as orchestrator

    duration_ms = 2_000

    def fake_probe(_path: Path, _runtime: RuntimeConfig) -> ProbeResult:
        return ProbeResult("audio", duration_ms, duration_ms * 16, {
            "timeline_status": "normal",
            "presentation_duration_ms": duration_ms,
            "presentation_total_samples": duration_ms * 16,
        })

    def fake_prepare(
        context: Any, source: Any, probe: ProbeResult, _runtime: RuntimeConfig
    ) -> MediaBundle:
        producer = Producer("fixture", "0.5.4", None, None, hash_json({"fixture": True}))
        source_ref = InputRef(
            role="source_media", source_asset_id=str(source["source_asset_id"])
        )
        probe_artifact = ArtifactEnvelope.create(
            artifact_kind="media_probe",
            scope_key="global",
            producer=producer,
            inputs=[source_ref],
            payload=probe.payload,
        )
        wav_path = context.store.temp_root / "timeline.wav"
        with wave.open(str(wav_path), "wb") as wav_file:
            wav_file.setnchannels(1)
            wav_file.setsampwidth(2)
            wav_file.setframerate(16_000)
            wav_file.writeframes(b"\0\0" * duration_ms * 16)
        blob_hash, length, _ = context.store.publish_blob(wav_path)
        timeline = ArtifactEnvelope.create(
            artifact_kind="timeline_audio",
            scope_key="global",
            producer=producer,
            inputs=[source_ref, InputRef(
                role="media_probe", artifact_id=probe_artifact.artifact_id
            )],
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
            context.publisher.publish(artifact)
        return MediaBundle(probe_artifact, timeline)

    monkeypatch.setattr(orchestrator, "probe_source", fake_probe)
    monkeypatch.setattr(orchestrator, "prepare_media", fake_prepare)
    FakeMediaStore.objects = {}

    config = trial_config(
        tmp_path,
        global_concurrency=1,
        visitor_concurrency=1,
        ip_concurrency=1,
        alive_interval_seconds=0.01,
    )
    initialized_store(config).close()
    media = config.work_root / ("job_" + "1" * 32) / "uploads" / "media.wav"
    media.parent.mkdir(parents=True)
    media.write_bytes(b"source")
    factories = ProviderFactories(
        media=FakeMediaStore,
        qwen=FakeQwenAsr,
        doubao=FakeDoubaoAsr,
        glm=lambda: (_ for _ in ()).throw(AssertionError("agreement called GLM")),
        qwen_correction=FakeQwenCorrection,
        kimi_correction=FakeKimiCorrection,
        ata=FakeAta,
        result_media=ResultStore,
    )
    service = TrialService(
        config,
        pricing=TrialPricing(config.pricing_path),
        storage_inspector=ReadyInspector(),
        source_store_factory=SourceStore,  # type: ignore[arg-type]
        provider_factories=factories,
        runtime=RuntimeConfig("ffmpeg", "ffprobe"),
        probe_factory=fake_probe,
    )
    job_id = "job_" + "1" * 32
    request_id = "req_" + "1" * 32
    try:
        accepted = service.submit(
            job_id=job_id,
            request_id=request_id,
            visitor_id="v_" + "1" * 32,
            ip_hmac="hmac-sha256:" + "a" * 64,
            fingerprint_hmac=None,
            media_path=media,
            references=(),
            keywords=("CueFlow",),
        )
        assert accepted["status"] in {"queued", "running", "succeeded"}
        service.wait_for_idle()
        store = TrialStore(config.database_path)
        try:
            row = store.request(request_id)
            assert row["execution_status"] == "succeeded"
            assert row["result_object_json"] is not None
            assert '"provider":"fake-result-store"' in row["result_object_json"]
            assert '"object_key":"trial/result/' in row["result_object_json"]
            assert row["provider_invocation_count"] > 0
            assert store.connection.execute(
                "SELECT COUNT(*) FROM trial_usage WHERE request_id=?", (request_id,)
            ).fetchone()[0] == row["provider_invocation_count"]
        finally:
            store.close()
        assert not (config.work_root / job_id).exists()
        assert service.result_url(job_id, "v_" + "1" * 32).startswith(
            "https://media.example/"
        )
    finally:
        service.close()
