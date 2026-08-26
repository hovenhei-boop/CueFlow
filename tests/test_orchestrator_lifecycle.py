from __future__ import annotations

import os
import wave
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

import cueflow.orchestrator as orchestrator_module
from cueflow.atomizer import atomize
from cueflow.config import RuntimeConfig, RuntimeDeviceConfig
from cueflow.errors import ContractError, DeliveryAmbiguousError, ExportBlockedError
from cueflow.orchestrator import (
    initialize_project,
    retry_invocation,
    run_project,
    set_project_glossary,
)
from cueflow.providers import AlignmentToken, SemanticResult


def _runtime() -> RuntimeConfig:
    ffmpeg = os.getenv("CUEFLOW_FFMPEG")
    ffprobe = os.getenv("CUEFLOW_FFPROBE")
    if not ffmpeg or not ffprobe:
        pytest.skip("real media lifecycle tests require CUEFLOW_FFMPEG and CUEFLOW_FFPROBE")
    return RuntimeConfig(
        ffmpeg=ffmpeg,
        ffprobe=ffprobe,
        model_cache=None,
        device=RuntimeDeviceConfig("cpu", "float32"),
    )


def _silent_wave(path: Path, duration_seconds: int) -> None:
    frames_remaining = duration_seconds * 16_000
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(16_000)
        block_frames = 16_000
        while frames_remaining:
            count = min(frames_remaining, block_frames)
            output.writeframesraw(b"\0\0" * count)
            frames_remaining -= count


class _Semantic:
    provider = "test-semantic"
    model = "test-verbatim"
    revision = "fixture"

    def __init__(self, text: str = "测试") -> None:
        self.text = text
        self.calls = 0
        self.closed = False

    def transcribe(
        self,
        audio_path: Path,
        glossary_terms: Sequence[str],
        *,
        rework_context: str | None = None,
    ) -> SemanticResult:
        assert audio_path.is_file()
        self.calls += 1
        return SemanticResult(self.text, "Chinese")

    def close(self) -> None:
        self.closed = True


class _Aligner:
    provider = "test-aligner"
    model = "Qwen3-ForcedAligner-0.6B-fixture"
    revision = "fixture"

    def __init__(self, *, fail_first: bool = False) -> None:
        self.fail_first = fail_first
        self.calls = 0
        self.closed = False

    def align(
        self, audio_path: Path, text: str, language: str | None
    ) -> list[AlignmentToken]:
        assert audio_path.is_file()
        self.calls += 1
        if self.fail_first and self.calls == 1:
            return [AlignmentToken("错", 0, 100)]
        _, atoms = atomize(text)
        return [
            AlignmentToken(str(atom["text"]), index * 100, (index + 1) * 100)
            for index, atom in enumerate(atoms)
        ]

    def close(self) -> None:
        self.closed = True


def _operation_rows(context: Any, run_id: str, operation: str) -> list[Any]:
    return [
        row
        for row in context.registry.invocations_for_run(run_id)
        if row["operation"] == operation
    ]


def test_four_semantic_attempts_precede_one_accepted_transcript_alignment(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.wav"
    _silent_wave(source, 2)
    context = initialize_project(tmp_path / "project", "Attempts")
    set_project_glossary(context, ["顾华玺"])
    provider_instances: list[_Semantic] = []
    aligner_instances: list[_Aligner] = []

    class VaryingSemantic(_Semantic):
        def transcribe(
            self,
            audio_path: Path,
            glossary_terms: Sequence[str],
            *,
            rework_context: str | None = None,
        ) -> SemanticResult:
            values = ("顾华西老师", "顾华喜老师", "顾华希老师", "顾华熙老师")
            result = SemanticResult(values[self.calls], "Chinese")
            self.calls += 1
            return result

    def semantic_factory() -> _Semantic:
        provider = VaryingSemantic()
        provider_instances.append(provider)
        return provider

    def aligner_factory(runtime: RuntimeConfig) -> _Aligner:
        aligner = _Aligner()
        aligner_instances.append(aligner)
        return aligner

    try:
        result = run_project(
            context,
            source,
            runtime=_runtime(),
            semantic_factory=semantic_factory,
            aligner_factory=aligner_factory,
        )
        run_id = str(result["run_id"])
        semantic_rows = _operation_rows(context, run_id, "semantic_transcription")
        alignment_rows = _operation_rows(context, run_id, "forced_alignment")
        assert len(semantic_rows) == 4
        assert len({row["artifact_id"] for row in semantic_rows}) == 4
        assert len(alignment_rows) == 1
        accepted_id = str(semantic_rows[-1]["artifact_id"])
        alignment_inputs = context.registry.invocation_inputs(
            str(alignment_rows[0]["invocation_id"])
        )
        assert [(row["role"], row["input_artifact_id"]) for row in alignment_inputs] == [
            ("media_chunk", context.current_artifact("media_chunk", "chunk_0001").artifact_id),
            ("transcript", accepted_id),
        ]
        assert context.current_artifact("transcript", "chunk_0001").artifact_id == accepted_id
        assert provider_instances[0].calls == 4
        assert provider_instances[0].closed
        assert aligner_instances[0].calls == 1
        assert aligner_instances[0].closed
    finally:
        context.close()


def test_factories_are_held_once_per_stage_across_multiple_chunks(tmp_path: Path) -> None:
    source = tmp_path / "long.wav"
    _silent_wave(source, 230)
    context = initialize_project(tmp_path / "project", "Stages")
    providers: list[_Semantic] = []
    aligners: list[_Aligner] = []

    def semantic_factory() -> _Semantic:
        provider = _Semantic()
        providers.append(provider)
        return provider

    def aligner_factory(runtime: RuntimeConfig) -> _Aligner:
        aligner = _Aligner()
        aligners.append(aligner)
        return aligner

    try:
        result = run_project(
            context,
            source,
            runtime=_runtime(),
            semantic_factory=semantic_factory,
            aligner_factory=aligner_factory,
        )
        assert len(context.current_artifact("chunk_plan").payload["chunks"]) == 2
        assert len(providers) == 1 and providers[0].calls == 2 and providers[0].closed
        assert len(aligners) == 1 and aligners[0].calls == 2 and aligners[0].closed
        assert len(_operation_rows(context, str(result["run_id"]), "forced_alignment")) == 2
    finally:
        context.close()


def test_execution_repair_and_one_batched_qa_repair_wave_are_independent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "long.wav"
    _silent_wave(source, 230)
    context = initialize_project(tmp_path / "project", "Repair waves")
    aligners: list[_Aligner] = []
    structural_calls = 0
    subtitle_ids: list[str] = []
    qa_ids: list[str] = []
    real_publish_subtitle = orchestrator_module._publish_subtitle
    real_publish_qa = orchestrator_module._publish_qa

    def aligner_factory(runtime: RuntimeConfig) -> _Aligner:
        aligner = _Aligner(fail_first=not aligners)
        aligners.append(aligner)
        return aligner

    def structural_fixture(
        context_value: Any,
        media: Any,
        transcripts: Sequence[Any],
        alignments: Sequence[Any],
        subtitle: Any,
    ) -> list[dict[str, Any]]:
        nonlocal structural_calls
        structural_calls += 1
        if structural_calls > 1:
            return []
        return [
            {
                "severity": "blocking_error",
                "code": "alignment_structural_error",
                "resolution_status": "unresolved",
                "locations": [],
                "observed": {"chunk_id": str(chunk.payload["chunk_id"]), "detail": "fixture"},
            }
            for chunk in media.media_chunks
        ]

    def publish_subtitle_spy(*args: Any, **kwargs: Any) -> Any:
        value = real_publish_subtitle(*args, **kwargs)
        subtitle_ids.append(value[0].artifact_id)
        return value

    def publish_qa_spy(*args: Any, **kwargs: Any) -> Any:
        value = real_publish_qa(*args, **kwargs)
        qa_ids.append(value.artifact_id)
        return value

    monkeypatch.setattr(orchestrator_module, "_structural_issues", structural_fixture)
    monkeypatch.setattr(orchestrator_module, "_publish_subtitle", publish_subtitle_spy)
    monkeypatch.setattr(orchestrator_module, "_publish_qa", publish_qa_spy)
    try:
        result = run_project(
            context,
            source,
            runtime=_runtime(),
            semantic_factory=lambda: _Semantic(),
            aligner_factory=aligner_factory,
        )
        run_id = str(result["run_id"])
        assert len(_operation_rows(context, run_id, "forced_alignment")) == 3
        assert len(_operation_rows(context, run_id, "qa_alignment_repair")) == 2
        assert context.registry.qa_repair_wave_count(run_id) == 1
        assert len(aligners) == 2
        assert [item.calls for item in aligners] == [3, 2]
        assert all(item.closed for item in aligners)
        assert len(subtitle_ids) == 2
        assert len(qa_ids) == 2 and qa_ids[0] != qa_ids[1]
        assert context.current_artifact("subtitle").artifact_id == subtitle_ids[-1]
        assert context.current_artifact("qa").artifact_id == qa_ids[-1]
    finally:
        context.close()


def test_qa_does_not_start_a_second_alignment_repair_wave(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source.wav"
    _silent_wave(source, 2)
    context = initialize_project(tmp_path / "project", "One QA wave")
    aligners: list[_Aligner] = []

    def aligner_factory(runtime: RuntimeConfig) -> _Aligner:
        aligner = _Aligner()
        aligners.append(aligner)
        return aligner

    def structural_fixture(
        context_value: Any,
        media: Any,
        transcripts: Sequence[Any],
        alignments: Sequence[Any],
        subtitle: Any,
    ) -> list[dict[str, Any]]:
        return [
            {
                "severity": "blocking_error",
                "code": "alignment_structural_error",
                "resolution_status": "unresolved",
                "locations": [],
                "observed": {"chunk_id": "chunk_0001", "detail": "fixture"},
            }
        ]

    monkeypatch.setattr(orchestrator_module, "_structural_issues", structural_fixture)
    try:
        with pytest.raises(ExportBlockedError, match="remained blocked"):
            run_project(
                context,
                source,
                runtime=_runtime(),
                semantic_factory=lambda: _Semantic(),
                aligner_factory=aligner_factory,
            )
        run = context.registry.latest_run(context.project_id)
        assert run is not None
        run_id = str(run["run_id"])
        assert len(_operation_rows(context, run_id, "qa_alignment_repair")) == 1
        assert len(aligners) == 2
    finally:
        context.close()


class _WindowFailureSemantic(_Semantic):
    total_calls = 0

    def transcribe(
        self,
        audio_path: Path,
        glossary_terms: Sequence[str],
        *,
        rework_context: str | None = None,
    ) -> SemanticResult:
        values = ("顾华西老师", "顾华喜老师", "顾华希老师")
        type(self).total_calls += 1
        if self.calls == 3:
            self.calls += 1
            raise DeliveryAmbiguousError("fixture delivery ambiguous")
        result = SemanticResult(values[self.calls], "Chinese")
        self.calls += 1
        return result


def test_targeted_retry_has_two_audited_resets_and_twelve_attempt_hard_cap(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.wav"
    _silent_wave(source, 2)
    context = initialize_project(tmp_path / "project", "Retry budgets")
    set_project_glossary(context, ["顾华玺"])
    _WindowFailureSemantic.total_calls = 0

    def semantic_factory() -> _WindowFailureSemantic:
        return _WindowFailureSemantic()

    try:
        with pytest.raises(DeliveryAmbiguousError):
            run_project(
                context,
                source,
                runtime=_runtime(),
                semantic_factory=semantic_factory,
                aligner_factory=lambda runtime: _Aligner(),
            )
        run = context.registry.latest_run(context.project_id)
        assert run is not None
        run_id = str(run["run_id"])
        source.unlink()

        for expected_window in (1, 2):
            failed = _operation_rows(context, run_id, "semantic_transcription")[-1]
            invocation_id = str(failed["invocation_id"])
            inputs = context.registry.invocation_inputs(invocation_id)
            assert [row["role"] for row in inputs] == ["media_chunk", "effective_glossary"]
            assert all(context.artifact(str(row["input_artifact_id"])) for row in inputs)
            with pytest.raises(DeliveryAmbiguousError):
                retry_invocation(
                    context,
                    invocation_id,
                    runtime=_runtime(),
                    semantic_factory=semantic_factory,
                    aligner_factory=lambda runtime: _Aligner(),
                )
            assert context.registry.semantic_budget_window(run_id, "chunk_0001") == expected_window
            assert context.registry.run(run_id)["status"] == "failed"

        third_target = str(
            _operation_rows(context, run_id, "semantic_transcription")[-1]["invocation_id"]
        )
        with pytest.raises(ContractError, match="semantic retry reset limit exhausted"):
            retry_invocation(
                context,
                third_target,
                runtime=_runtime(),
                semantic_factory=semantic_factory,
                aligner_factory=lambda runtime: _Aligner(),
            )
        semantic_rows = _operation_rows(context, run_id, "semantic_transcription")
        assert len(semantic_rows) == 12
        assert _WindowFailureSemantic.total_calls == 12
        assert [
            context.registry.sent_semantic_attempt_count(run_id, "chunk_0001", window)
            for window in range(3)
        ] == [4, 4, 4]
        assert {int(row["semantic_budget_window"]) for row in semantic_rows} == {0, 1, 2}
        assert _operation_rows(context, run_id, "forced_alignment") == []
    finally:
        context.close()


class _TwoChunkFactory:
    def __init__(self) -> None:
        self.created = 0
        self.calls_by_instance: list[int] = []

    def __call__(self) -> _Semantic:
        factory = self
        instance_number = self.created
        self.created += 1

        class Provider(_Semantic):
            def transcribe(
                self,
                audio_path: Path,
                glossary_terms: Sequence[str],
                *,
                rework_context: str | None = None,
            ) -> SemanticResult:
                self.calls += 1
                if instance_number == 0 and self.calls == 2:
                    factory.calls_by_instance.append(self.calls)
                    raise DeliveryAmbiguousError("second chunk failed")
                return SemanticResult("测试", "Chinese")

            def close(self) -> None:
                if not factory.calls_by_instance or factory.calls_by_instance[-1] != self.calls:
                    factory.calls_by_instance.append(self.calls)
                super().close()

        return Provider()


def test_targeted_retry_uses_bound_artifacts_and_skips_other_successful_chunk(
    tmp_path: Path,
) -> None:
    source = tmp_path / "long.wav"
    _silent_wave(source, 230)
    context = initialize_project(tmp_path / "project", "Targeted")
    semantic_factory = _TwoChunkFactory()
    try:
        with pytest.raises(DeliveryAmbiguousError):
            run_project(
                context,
                source,
                runtime=_runtime(),
                semantic_factory=semantic_factory,
                aligner_factory=lambda runtime: _Aligner(),
            )
        run = context.registry.latest_run(context.project_id)
        assert run is not None
        run_id = str(run["run_id"])
        original_rows = _operation_rows(context, run_id, "semantic_transcription")
        first_artifact = str(original_rows[0]["artifact_id"])
        target_id = str(original_rows[-1]["invocation_id"])
        original_target_inputs = [
            (row["role"], row["input_artifact_id"])
            for row in context.registry.invocation_inputs(target_id)
        ]
        source.unlink()
        result = retry_invocation(
            context,
            target_id,
            runtime=_runtime(),
            semantic_factory=semantic_factory,
            aligner_factory=lambda runtime: _Aligner(),
        )
        assert result["run_id"] == run_id
        assert context.registry.run(run_id)["status"] == "succeeded"
        all_rows = _operation_rows(context, run_id, "semantic_transcription")
        chunk_one_rows = [row for row in all_rows if row["chunk_id"] == "chunk_0001"]
        chunk_two_rows = [row for row in all_rows if row["chunk_id"] == "chunk_0002"]
        assert len(chunk_one_rows) == 1 and chunk_one_rows[0]["artifact_id"] == first_artifact
        assert len(chunk_two_rows) == 2
        retry_inputs = [
            (row["role"], row["input_artifact_id"])
            for row in context.registry.invocation_inputs(str(chunk_two_rows[-1]["invocation_id"]))
        ]
        assert retry_inputs == original_target_inputs
        assert semantic_factory.calls_by_instance == [2, 1]
    finally:
        context.close()


def test_new_run_reexecutes_semantic_and_alignment(tmp_path: Path) -> None:
    source = tmp_path / "source.wav"
    _silent_wave(source, 2)
    context = initialize_project(tmp_path / "project", "Fresh execution")
    providers: list[_Semantic] = []
    aligners: list[_Aligner] = []

    def semantic_factory() -> _Semantic:
        instance = _Semantic()
        providers.append(instance)
        return instance

    def aligner_factory(runtime: RuntimeConfig) -> _Aligner:
        instance = _Aligner()
        aligners.append(instance)
        return instance

    try:
        first = run_project(
            context,
            source,
            runtime=_runtime(),
            semantic_factory=semantic_factory,
            aligner_factory=aligner_factory,
        )
        first_timeline_hash = str(
            context.current_artifact("timeline_audio").payload["audio_blob"]["content_hash"]
        )
        _silent_wave(source, 3)
        second = run_project(
            context,
            source,
            runtime=_runtime(),
            semantic_factory=semantic_factory,
            aligner_factory=aligner_factory,
        )
        assert first["run_id"] != second["run_id"]
        assert first["source_asset_id"] == second["source_asset_id"]
        assert (
            context.current_artifact("timeline_audio").payload["audio_blob"]["content_hash"]
            != first_timeline_hash
        )
        source_row = context.registry.source_asset(
            context.project_id, str(second["source_asset_id"])
        )
        assert source_row["filename"] == source.name
        assert "content_hash" not in source_row.keys()
        assert "byte_length" not in source_row.keys()
        second_run = context.registry.run(str(second["run_id"]))
        assert "content_hash" not in str(second_run["input_identity_json"])
        assert "storage_locator" not in str(second_run["input_identity_json"])
        assert len(providers) == 2 and [item.calls for item in providers] == [1, 1]
        assert len(aligners) == 2 and [item.calls for item in aligners] == [1, 1]
        assert len(_operation_rows(context, str(first["run_id"]), "semantic_transcription")) == 1
        assert len(_operation_rows(context, str(second["run_id"]), "semantic_transcription")) == 1
    finally:
        context.close()
