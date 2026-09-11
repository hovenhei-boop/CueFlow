from __future__ import annotations

import json
from typing import Any

import pytest
from test_orchestrator_v052 import (
    FakeAta,
    FakeDoubaoAsr,
    FakeGlm,
    FakeKimiCorrection,
    FakeMediaStore,
    FakeQwenAsr,
    FakeQwenCorrection,
    _project_with_fake_media,
)

from cueflow.asr_contracts import AsrResult, ProviderMetadata, TimedUnit
from cueflow.ata_provider import AtaResponse
from cueflow.config import SelectionConfig
from cueflow.conflict_selection import build_selection_batches
from cueflow.correction_provider import CorrectionResult
from cueflow.errors import (
    ContractError,
    DeliveryAmbiguousError,
    IntegrityError,
    ProviderError,
    SrtSerializationError,
)
from cueflow.glm_selection_provider import SelectionResult
from cueflow.orchestrator import (
    resolve_review,
    resume_run,
    retry_invocation,
    retry_run,
    run_project,
)
from cueflow.project import single_writer

SENTENCES = [("前文" * 20 + token + "后文" * 20 + "。") for token in ("A0", "B0", "C0")]
BASE = "".join(SENTENCES)


class LongAsr:
    provider = "fixture"
    model = "long"

    def transcribe(self, url: str, **kwargs: Any) -> AsrResult:
        return AsrResult(
            BASE,
            tuple(
                TimedUnit(text, i * 40_000, i * 40_000 + 2_000) for i, text in enumerate(SENTENCES)
            ),
            ProviderMetadata(self.provider, self.model),
        )

    def close(self) -> None:
        pass


class LongCorrection:
    provider = "fixture-correction"
    model = "long"
    arm = "qwen"
    calls = 0

    def correct(self, request: Any) -> CorrectionResult:
        type(self).calls += 1
        assert request.base_text == BASE and request.peer_text == BASE
        assert not hasattr(request, "glm_evidence")
        return CorrectionResult(
            BASE.replace("0", "1"),
            ProviderMetadata(self.provider, self.model),
        )

    def close(self) -> None:
        pass


class LongKimi(LongCorrection):
    arm = "kimi"

    def correct(self, request: Any) -> CorrectionResult:
        return CorrectionResult(BASE.replace("0", "2"), ProviderMetadata(self.provider, self.model))


def _one_case_batches(monkeypatch: Any) -> None:
    monkeypatch.setattr(
        "cueflow.orchestrator.build_selection_batches",
        lambda plan: build_selection_batches(plan, SelectionConfig(max_cases=1)),
    )


class WindowGlm:
    provider = "fixture-glm"
    model = "glm-5.2"
    calls: list[int] = []
    failures: set[int] = {1}
    next_window: list[int] = [0, 1, 2]

    def select(self, request: Any) -> SelectionResult:
        assert set(request) == {"cases"}
        assert len(request["cases"]) == 1
        index = type(self).next_window.pop(0)
        type(self).calls.append(index)
        if index in self.failures:
            raise TimeoutError("batch timeout")
        case = request["cases"][0]
        candidate = next(c for c in case["candidates"] if c["text"] == "1")
        return SelectionResult(
            (dict(case_id=case["case_id"], candidate_id=candidate["candidate_id"]),),
            ProviderMetadata(self.provider, self.model),
        )

    def close(self) -> None:
        pass


class TextAta:
    provider = "fixture-ata"
    model = "ata"
    texts: list[str] = []

    def align(self, url: str, text: str) -> AtaResponse:
        type(self).texts.append(text)
        return AtaResponse(
            json.dumps({"code": 0, "utterances": [
                {"text": text, "start_time": 0, "end_time": 1_800},
            ]}, ensure_ascii=False).encode("utf-8"),
            text,
            ProviderMetadata(self.provider, self.model),
        )

    def close(self) -> None:
        pass


def _long_run(tmp_path: Any, monkeypatch: Any, failures: set[int]) -> tuple[Any, dict]:
    _one_case_batches(monkeypatch)
    path, context = _project_with_fake_media(tmp_path, monkeypatch, duration_ms=100_000)
    WindowGlm.calls, WindowGlm.failures, WindowGlm.next_window = [], failures, [0, 1, 2]
    LongCorrection.calls, TextAta.texts = 0, []
    pending = run_project(
        context,
        path,
        keywords=["NVIDIA"],
        media_store_factory=FakeMediaStore,
        qwen_asr_factory=LongAsr,
        doubao_asr_factory=LongAsr,
        qwen_correction_factory=LongCorrection,
        kimi_correction_factory=LongKimi,
        glm_selection_factory=WindowGlm,
        ata_factory=TextAta,
    )
    return context, pending


def test_one_failed_window_does_not_block_other_windows_then_human_keep(
    tmp_path: Any,
    monkeypatch: Any,
) -> None:
    context, pending = _long_run(tmp_path, monkeypatch, {1})
    try:
        assert WindowGlm.calls == [0, 1, 2]
        assert pending["status"] == "needs_review"
        queue = context.current_artifact("review_queue")
        assert len(queue.payload["items"]) == 1
        assert not TextAta.texts
        result = resolve_review(
            context,
            [{"review_id": queue.payload["items"][0]["review_id"], "action": "keep"}],
            run_id=pending["run_id"],
            expected_review_queue_artifact_id=queue.artifact_id,
            media_store_factory=FakeMediaStore,
            ata_factory=TextAta,
        )
        assert result["status"] == "succeeded"
        assert TextAta.texts == [BASE.replace("A0", "A1").replace("C0", "C1")]
        assert (
            context.current_artifact("review_resolution").payload["decisions"][0]["action"]
            == "keep"
        )
        failed = next(
            row
            for row in context.registry.invocations_for_run(pending["run_id"])
            if row["operation"] == "glm_selection" and row["status"] != "succeeded"
        )
        with pytest.raises(ContractError):
            retry_invocation(context, failed["invocation_id"])
        assert WindowGlm.calls == [0, 1, 2]
    finally:
        context.close()


def test_glm_targeted_retry_never_repeats_correction_or_other_windows(
    tmp_path: Any,
    monkeypatch: Any,
) -> None:
    context, pending = _long_run(tmp_path, monkeypatch, {1})
    try:
        failed = next(
            row
            for row in context.registry.invocations_for_run(pending["run_id"])
            if row["operation"] == "glm_selection" and row["status"] != "succeeded"
        )
        WindowGlm.failures, WindowGlm.next_window = set(), [1]
        outcome = retry_invocation(
            context,
            failed["invocation_id"],
            glm_selection_factory=WindowGlm,
            media_store_factory=FakeMediaStore,
            ata_factory=TextAta,
        )
        assert outcome["status"] == "succeeded"
        assert WindowGlm.calls == [0, 1, 2, 1]
        assert LongCorrection.calls == 1
        assert TextAta.texts == [BASE.replace("0", "1")]
    finally:
        context.close()


def test_all_glm_failures_reach_review_and_can_finish(tmp_path: Any, monkeypatch: Any) -> None:
    context, pending = _long_run(tmp_path, monkeypatch, {0, 1, 2})
    try:
        queue = context.current_artifact("review_queue")
        assert len(queue.payload["items"]) == 3
        result = resolve_review(
            context,
            [{"review_id": item["review_id"], "action": "qwen"} for item in queue.payload["items"]],
            run_id=pending["run_id"],
            expected_review_queue_artifact_id=queue.artifact_id,
            media_store_factory=FakeMediaStore,
            ata_factory=TextAta,
        )
        assert result["status"] == "succeeded"
        assert WindowGlm.calls == [0, 1, 2]
    finally:
        context.close()


class FailKimi(FakeKimiCorrection):
    def correct(self, request: Any) -> CorrectionResult:
        raise ProviderError("Kimi failed")


def test_failed_arm_retry_reuses_success_and_retry_run_is_a_new_round(
    tmp_path: Any,
    monkeypatch: Any,
) -> None:
    path, context = _project_with_fake_media(tmp_path, monkeypatch)
    FakeQwenCorrection.requests, FakeKimiCorrection.requests = [], []
    try:
        with pytest.raises(ProviderError):
            run_project(
                context,
                path,
                media_store_factory=FakeMediaStore,
                qwen_asr_factory=FakeQwenAsr,
                doubao_asr_factory=FakeDoubaoAsr,
                qwen_correction_factory=FakeQwenCorrection,
                kimi_correction_factory=FailKimi,
            )
        run_id = context.registry.run(context.run_id)["run_id"]
        failed = context.registry.invocations_for_run(run_id)[-1]
        retry_invocation(
            context,
            failed["invocation_id"],
            media_store_factory=FakeMediaStore,
            kimi_correction_factory=FakeKimiCorrection,
            ata_factory=FakeAta,
        )
        assert len(FakeQwenCorrection.requests) == len(FakeKimiCorrection.requests) == 1
        new_run = retry_run(
            context, run_id,
            media_store_factory=FakeMediaStore,
            qwen_correction_factory=FakeQwenCorrection,
            kimi_correction_factory=FakeKimiCorrection,
            ata_factory=FakeAta,
        )
        assert new_run["run_id"] == run_id
        assert new_run["execution_round"] == 2
        assert len(FakeQwenCorrection.requests) == len(FakeKimiCorrection.requests) == 2
        operations = [
            row["operation"] for row in context.registry.invocations_for_run(new_run["run_id"])
            if row["execution_round"] == 2
        ]
        assert operations == ["qwen_correction", "kimi_correction", "ata"]
    finally:
        context.close()


def test_plan_checkpoint_survives_crash_before_glm(tmp_path: Any, monkeypatch: Any) -> None:
    import cueflow.orchestrator as orchestrator

    _one_case_batches(monkeypatch)
    path, context = _project_with_fake_media(tmp_path, monkeypatch, duration_ms=100_000)
    original = orchestrator._select_batch
    monkeypatch.setattr(
        orchestrator, "_select_batch", lambda *args: (_ for _ in ()).throw(KeyboardInterrupt())
    )
    WindowGlm.calls, WindowGlm.failures, WindowGlm.next_window = [], set(), [0, 1, 2]
    LongCorrection.calls, TextAta.texts = 0, []
    try:
        with pytest.raises(KeyboardInterrupt):
            run_project(
                context,
                path,
                keywords=["NVIDIA"],
                media_store_factory=FakeMediaStore,
                qwen_asr_factory=LongAsr,
                doubao_asr_factory=LongAsr,
                qwen_correction_factory=LongCorrection,
                kimi_correction_factory=LongKimi,
                glm_selection_factory=WindowGlm,
            )
        run_id = context.registry.run(context.run_id)["run_id"]
        assert len(context.current_artifact("merge_plan").payload["cases"]) == 3
        assert not WindowGlm.calls
        monkeypatch.setattr(orchestrator, "_select_batch", original)
        result = resume_run(
            context,
            run_id,
            media_store_factory=FakeMediaStore,
            glm_selection_factory=WindowGlm,
            ata_factory=TextAta,
        )
        assert result["status"] == "succeeded"
        assert LongCorrection.calls == 1
        assert WindowGlm.calls == [0, 1, 2]
    finally:
        context.close()


def test_stale_review_and_config_changes_reject_before_paid_calls(
    tmp_path: Any,
    monkeypatch: Any,
) -> None:
    import cueflow.orchestrator as orchestrator

    context, pending = _long_run(tmp_path, monkeypatch, {1})
    try:
        queue = context.current_artifact("review_queue")
        with pytest.raises(ContractError, match="fields do not match"):
            resolve_review(
                context,
                [
                    {
                        "review_id": queue.payload["items"][0]["review_id"],
                        "action": "replace",
                        "start": 0,
                        "end": 1,
                        "replacement": "X",
                    }
                ],
                run_id=pending["run_id"],
                expected_review_queue_artifact_id=queue.artifact_id,
            )
        with pytest.raises(ContractError, match="stale"):
            resolve_review(
                context, [], run_id=pending["run_id"], expected_review_queue_artifact_id="wrong"
            )
        monkeypatch.setattr(orchestrator, "_config_hash", lambda: "changed")
        with pytest.raises(ContractError, match="identity/config/prompt"):
            resume_run(context, pending["run_id"])
        assert WindowGlm.calls == [0, 1, 2]
    finally:
        context.close()


def test_human_insertion_uses_frozen_review_interval_without_text_search(
    tmp_path: Any,
    monkeypatch: Any,
) -> None:
    class InsertionAsr:
        provider = "fixture-asr"
        model = "fixture"

        def transcribe(self, url: str, **kwargs: Any) -> AsrResult:
            return AsrResult(
                "AB",
                (TimedUnit("AB", 0, 1_000),),
                ProviderMetadata(self.provider, self.model),
            )

        def close(self) -> None:
            pass

    class QwenInsertion(FakeQwenCorrection):
        def correct(self, request: Any) -> CorrectionResult:
            return CorrectionResult("AXB", ProviderMetadata(self.provider, self.model))

    class KimiInsertion(FakeKimiCorrection):
        def correct(self, request: Any) -> CorrectionResult:
            return CorrectionResult("AYB", ProviderMetadata(self.provider, self.model))

    path, context = _project_with_fake_media(tmp_path, monkeypatch)
    TextAta.texts = []
    try:
        pending = run_project(
            context,
            path,
            media_store_factory=FakeMediaStore,
            qwen_asr_factory=InsertionAsr,
            doubao_asr_factory=InsertionAsr,
            qwen_correction_factory=QwenInsertion,
            kimi_correction_factory=KimiInsertion,
            glm_selection_factory=FakeGlm,
        )
        queue = context.current_artifact("review_queue")
        item = queue.payload["items"][0]
        assert (item["start"], item["end"], item["original"]) == (1, 1, "")
        decision = {"review_id": item["review_id"], "action": "replace", "replacement": "Z"}
        result = resolve_review(
            context,
            [decision],
            run_id=pending["run_id"],
            expected_review_queue_artifact_id=queue.artifact_id,
            media_store_factory=FakeMediaStore,
            ata_factory=TextAta,
        )
        assert result["status"] == "succeeded"
        assert TextAta.texts == ["AZB"]
        assert context.current_artifact("review_resolution").payload["decisions"] == [decision]
    finally:
        context.close()


def test_writer_excludes_second_writer(tmp_path: Any, monkeypatch: Any) -> None:
    _, context = _project_with_fake_media(tmp_path, monkeypatch)

    @single_writer
    def first(inner: Any) -> None:
        with pytest.raises(ContractError, match="another CueFlow writer"):
            second(inner)

    @single_writer
    def second(inner: Any) -> None:
        raise AssertionError("lock was not exclusive")

    try:
        first(context)
    finally:
        context.close()


def test_plain_resume_keeps_terminal_review_revision_and_does_not_retry(
    tmp_path: Any,
    monkeypatch: Any,
) -> None:
    context, pending = _long_run(tmp_path, monkeypatch, {1})

    def unavailable_store() -> Any:
        raise AssertionError("checkpoint/GLM-only resume must not presign media")

    try:
        resumed = resume_run(context, pending["run_id"], media_store_factory=unavailable_store)
        assert resumed == pending
        assert WindowGlm.calls == [0, 1, 2]
        assert LongCorrection.calls == 1
    finally:
        context.close()


def test_interruption_during_glm_resumes_other_windows_without_ambiguous_replay(
    tmp_path: Any,
    monkeypatch: Any,
) -> None:
    _one_case_batches(monkeypatch)
    path, context = _project_with_fake_media(tmp_path, monkeypatch, duration_ms=100_000)

    class InterruptedGlm(WindowGlm):
        def select(self, request: Any) -> SelectionResult:
            raise KeyboardInterrupt()

    WindowGlm.calls, WindowGlm.failures, WindowGlm.next_window = [], set(), [1, 2]
    try:
        with pytest.raises(KeyboardInterrupt):
            run_project(
                context,
                path,
                keywords=["NVIDIA"],
                media_store_factory=FakeMediaStore,
                qwen_asr_factory=LongAsr,
                doubao_asr_factory=LongAsr,
                qwen_correction_factory=LongCorrection,
                kimi_correction_factory=LongKimi,
                glm_selection_factory=InterruptedGlm,
            )
        run_id = context.registry.run(context.run_id)["run_id"]
        resumed = resume_run(
            context, run_id, media_store_factory=FakeMediaStore, glm_selection_factory=WindowGlm
        )
        assert resumed["status"] == "needs_review"
        assert resumed["review_item_count"] == 1
        assert WindowGlm.calls == [1, 2]
        glm = [
            row
            for row in context.registry.invocations_for_run(run_id)
            if row["operation"] == "glm_selection"
        ]
        assert [row["status"] for row in glm] == [
            "delivery_ambiguous",
            "succeeded",
            "succeeded",
        ]
    finally:
        context.close()


def test_final_and_queue_checkpoint_roll_back_together(tmp_path: Any, monkeypatch: Any) -> None:
    import cueflow.orchestrator as orchestrator

    original_save = orchestrator._save

    def fail_queue(context: Any, run_id: str, kind: str, *args: Any, **kwargs: Any) -> Any:
        if kind == "review_queue":
            raise RuntimeError("simulated failure before queue commit")
        return original_save(context, run_id, kind, *args, **kwargs)

    path, context = _project_with_fake_media(tmp_path, monkeypatch)
    monkeypatch.setattr(orchestrator, "_save", fail_queue)
    try:
        with pytest.raises(RuntimeError, match="queue commit"):
            run_project(
                context,
                path,
                keywords=["NVIDIA"],
                media_store_factory=FakeMediaStore,
                qwen_asr_factory=FakeQwenAsr,
                doubao_asr_factory=FakeDoubaoAsr,
                qwen_correction_factory=FakeQwenCorrection,
                kimi_correction_factory=FakeKimiCorrection,
            )
        run_id = context.registry.run(context.run_id)["run_id"]
        assert context.registry.checkpoint(run_id, "edit_resolution") is None
        assert context.registry.checkpoint(run_id, "review_queue") is None
        monkeypatch.setattr(orchestrator, "_save", original_save)
        result = resume_run(
            context, run_id, media_store_factory=FakeMediaStore, ata_factory=FakeAta
        )
        assert result["status"] == "succeeded"
    finally:
        context.close()


def test_resume_after_ata_commit_does_not_repeat_ata(tmp_path: Any, monkeypatch: Any) -> None:
    import cueflow.orchestrator as orchestrator

    original_downstream = orchestrator._publish_downstream
    calls: list[str] = []

    class CountingAta(FakeAta):
        def align(self, url: str, text: str) -> AtaResponse:
            calls.append(text)
            return super().align(url, text)

    def interrupt(*args: Any, **kwargs: Any) -> Any:
        raise KeyboardInterrupt()

    path, context = _project_with_fake_media(tmp_path, monkeypatch)
    monkeypatch.setattr(orchestrator, "_publish_downstream", interrupt)
    try:
        with pytest.raises(KeyboardInterrupt):
            run_project(
                context,
                path,
                keywords=["NVIDIA"],
                media_store_factory=FakeMediaStore,
                qwen_asr_factory=FakeQwenAsr,
                doubao_asr_factory=FakeDoubaoAsr,
                qwen_correction_factory=FakeQwenCorrection,
                kimi_correction_factory=FakeKimiCorrection,
                ata_factory=CountingAta,
            )
        run_id = context.registry.run(context.run_id)["run_id"]
        assert context.registry.checkpoint(run_id, "ata_response") is not None
        monkeypatch.setattr(orchestrator, "_publish_downstream", original_downstream)
        result = resume_run(context, run_id, media_store_factory=FakeMediaStore)
        assert result["status"] == "succeeded"
        assert len(calls) == 1
    finally:
        context.close()


def test_corrupted_window_is_integrity_failure_not_local_review(
    tmp_path: Any,
    monkeypatch: Any,
) -> None:
    import cueflow.orchestrator as orchestrator

    context, pending = _long_run(tmp_path, monkeypatch, {1})
    original_get = orchestrator._get

    def corrupt(context: Any, run_id: str, kind: str, *args: Any) -> Any:
        if kind == "selection_batch":
            raise IntegrityError("corrupt selection batch blob")
        return original_get(context, run_id, kind, *args)

    failed = next(
        row
        for row in context.registry.invocations_for_run(pending["run_id"])
        if row["operation"] == "glm_selection" and row["status"] != "succeeded"
    )
    monkeypatch.setattr(orchestrator, "_get", corrupt)
    try:
        with pytest.raises(IntegrityError, match="corrupt selection batch"):
            retry_invocation(context, failed["invocation_id"], media_store_factory=FakeMediaStore)
        assert WindowGlm.calls == [0, 1, 2]
    finally:
        context.close()


def test_paid_result_checkpoint_and_current_pointer_are_one_commit(
    tmp_path: Any,
    monkeypatch: Any,
) -> None:
    from cueflow.registry import Registry

    original = Registry._checkpoint_tx

    def crash(self: Any, tx: Any, run_id: str, stage: str, *args: Any) -> None:
        original(self, tx, run_id, stage, *args)
        if stage == "base_asr":
            raise RuntimeError("crash between checkpoint and invocation success")

    path, context = _project_with_fake_media(tmp_path, monkeypatch)
    monkeypatch.setattr(Registry, "_checkpoint_tx", crash)
    try:
        with pytest.raises(RuntimeError, match="checkpoint and invocation"):
            run_project(
                context, path, media_store_factory=FakeMediaStore, qwen_asr_factory=FakeQwenAsr
            )
        run_id = context.registry.run(context.run_id)["run_id"]
        assert context.registry.checkpoint(run_id, "base_asr") is None
        assert context.registry.current_pointer(context.run_id, "base_asr", "global") is None
        invocation = context.registry.invocations_for_run(run_id)[-1]
        assert invocation["status"] == "delivery_ambiguous"
        assert invocation["artifact_id"] is None
        assert (
            context.registry.connection.execute(
                "SELECT COUNT(*) FROM artifacts WHERE artifact_kind='base_asr'"
            ).fetchone()[0]
            == 0
        )
        monkeypatch.setattr(Registry, "_checkpoint_tx", original)
        with pytest.raises(ProviderError, match="explicit retry"):
            resume_run(context, run_id, media_store_factory=FakeMediaStore)
    finally:
        context.close()


def test_parallel_corrections_have_one_database_writer_and_preserve_success(
    tmp_path: Any,
    monkeypatch: Any,
) -> None:
    import threading

    from cueflow.registry import Registry

    barrier = threading.Barrier(2)
    main_thread = threading.get_ident()
    published_threads: list[int] = []
    original = Registry.publish_artifact

    def publish(self: Any, **kwargs: Any) -> None:
        published_threads.append(threading.get_ident())
        original(self, **kwargs)

    class ParallelQwen(FakeQwenCorrection):
        def correct(self, request: Any) -> CorrectionResult:
            barrier.wait(timeout=3)
            return super().correct(request)

    class ParallelKimi(FakeKimiCorrection):
        def correct(self, request: Any) -> CorrectionResult:
            barrier.wait(timeout=3)
            raise ProviderError("second arm failed")

    path, context = _project_with_fake_media(tmp_path, monkeypatch)
    monkeypatch.setattr(Registry, "publish_artifact", publish)
    try:
        with pytest.raises(ProviderError, match="second arm"):
            run_project(
                context,
                path,
                media_store_factory=FakeMediaStore,
                qwen_asr_factory=FakeQwenAsr,
                doubao_asr_factory=FakeDoubaoAsr,
                qwen_correction_factory=ParallelQwen,
                kimi_correction_factory=ParallelKimi,
            )
        run_id = context.registry.run(context.run_id)["run_id"]
        assert context.registry.checkpoint(run_id, "correction_transcript", "qwen") is not None
        assert context.registry.checkpoint(run_id, "correction_transcript", "kimi") is None
        assert set(published_threads) == {main_thread}
    finally:
        context.close()


def test_ambiguous_correction_metadata_is_persisted(
    tmp_path: Any,
    monkeypatch: Any,
) -> None:
    import json

    class AmbiguousQwen(FakeQwenCorrection):
        def correct(self, request: Any) -> CorrectionResult:
            raise DeliveryAmbiguousError(
                "fixture stream interrupted",
                metadata=ProviderMetadata(
                    self.provider,
                    self.model,
                    resolved_model="fixture-resolved",
                    response_id="fixture-response",
                    elapsed_ms=321,
                    usage={"total_tokens": 17},
                ),
            )

    path, context = _project_with_fake_media(tmp_path, monkeypatch)
    try:
        with pytest.raises(DeliveryAmbiguousError, match="fixture stream interrupted"):
            run_project(
                context,
                path,
                media_store_factory=FakeMediaStore,
                qwen_asr_factory=FakeQwenAsr,
                doubao_asr_factory=FakeDoubaoAsr,
                qwen_correction_factory=AmbiguousQwen,
                kimi_correction_factory=FakeKimiCorrection,
            )
        run_id = context.registry.run(context.run_id)["run_id"]
        row = next(
            item
            for item in context.registry.invocations_for_run(run_id)
            if item["operation"] == "qwen_correction"
        )
        assert row["status"] == "delivery_ambiguous"
        assert row["resolved_model"] == "fixture-resolved"
        assert row["response_id"] == "fixture-response"
        assert row["elapsed_ms"] == 321
        assert json.loads(row["usage_json"]) == {"total_tokens": 17}
        assert row["error_message"] == "fixture stream interrupted"
    finally:
        context.close()


def test_ata_serialization_failure_preserves_success_metadata_and_result(
    tmp_path: Any,
    monkeypatch: Any,
) -> None:
    import json

    class InvalidTimeAta(FakeAta):
        def align(self, media_url: str, transcript_text: str) -> AtaResponse:
            assert transcript_text == "Qwen3.8 is good."
            return AtaResponse(
                json.dumps({"code": 0, "utterances": [
                    {"text": transcript_text, "start_time": 1_500, "end_time": 1_200},
                ]}).encode("utf-8"),
                transcript_text,
                ProviderMetadata(
                    self.provider,
                    self.model,
                    resolved_model="ata-resolved",
                    response_id="ata-task",
                    elapsed_ms=37,
                    usage={"duration": 2},
                ),
            )

    path, context = _project_with_fake_media(tmp_path, monkeypatch)
    try:
        with pytest.raises(SrtSerializationError, match="negative or reversed"):
            run_project(
                context,
                path,
                media_store_factory=FakeMediaStore,
                qwen_asr_factory=FakeQwenAsr,
                doubao_asr_factory=FakeDoubaoAsr,
                qwen_correction_factory=FakeQwenCorrection,
                kimi_correction_factory=FakeKimiCorrection,
                ata_factory=InvalidTimeAta,
            )
        run_id = context.registry.run(context.run_id)["run_id"]
        row = next(
            item
            for item in context.registry.invocations_for_run(run_id)
            if item["operation"] == "ata"
        )
        assert row["status"] == "succeeded"
        assert row["resolved_model"] == "ata-resolved"
        assert row["response_id"] == "ata-task"
        assert row["elapsed_ms"] == 37
        assert json.loads(row["usage_json"]) == {"duration": 2}
        assert context.registry.checkpoint(run_id, "ata_response") is not None
        assert context.registry.checkpoint(run_id, "ata_result") is not None
        assert context.current_artifact("ata_result").payload["diagnostics"][
            "reversed_interval_count"
        ] == 1
        assert context.registry.run(run_id)["status"] == "failed"
        assert not (context.root / "output" / "subtitles.srt").exists()
    finally:
        context.close()


def test_completed_format_retry_has_two_billing_records_and_identical_request(
    tmp_path: Any,
    monkeypatch: Any,
) -> None:
    import json

    from cueflow.cloud_stream import CompletedResponseError

    requests: list[Any] = []

    class FormatQwen(FakeQwenCorrection):
        def correct(self, request: Any) -> CorrectionResult:
            requests.append(request)
            if len(requests) == 1:
                raise CompletedResponseError(
                    "bad JSON",
                    ProviderMetadata(
                        self.provider,
                        self.model,
                        response_id="paid-first",
                        reasoning_ms=17,
                        usage={"total_tokens": 123},
                        search_results=({"title": "fixture evidence"},),
                    ),
                    raw_response='{"corrected_text":""}',
                    finish_reason="stop",
                )
            return CorrectionResult(
                "Qwen3.8 is good.",
                ProviderMetadata(
                    self.provider,
                    self.model,
                    response_id="paid-second",
                    usage={"total_tokens": 456},
                ),
            )

    path, context = _project_with_fake_media(tmp_path, monkeypatch)
    try:
        result = run_project(
            context,
            path,
            media_store_factory=FakeMediaStore,
            qwen_asr_factory=FakeQwenAsr,
            doubao_asr_factory=FakeDoubaoAsr,
            qwen_correction_factory=FormatQwen,
            kimi_correction_factory=FakeKimiCorrection,
            ata_factory=FakeAta,
        )
        assert result["status"] == "succeeded"
        rows = [
            row
            for row in context.registry.invocations_for_run(result["run_id"])
            if row["operation"] == "qwen_correction"
        ]
        assert requests[0] == requests[1] and len(requests) == 2
        assert [row["status"] for row in rows] == ["explicit_failure", "succeeded"]
        assert [json.loads(row["usage_json"])["total_tokens"] for row in rows] == [123, 456]
        diagnostic = json.loads(rows[0]["diagnostic_json"])
        assert diagnostic["raw_response"] == '{"corrected_text":""}'
        assert diagnostic["finish_reason"] == "stop"
        assert diagnostic["search_results"] == [{"title": "fixture evidence"}]
        assert rows[0]["reasoning_ms"] == 17
        assert rows[1]["retry_of_invocation_id"] == rows[0]["invocation_id"]
        assert rows[1]["prompt_sha256"] == rows[0]["prompt_sha256"]
        assert [
            row["input_artifact_id"]
            for row in context.registry.invocation_inputs(rows[0]["invocation_id"])
        ] == [
            row["input_artifact_id"]
            for row in context.registry.invocation_inputs(rows[1]["invocation_id"])
        ]
    finally:
        context.close()


def test_invalid_glm_format_retries_once_without_fabricating_keep(
    tmp_path: Any, monkeypatch: Any
) -> None:
    import cueflow.orchestrator as orchestrator
    from cueflow.cloud_stream import CompletedResponseError

    requests: list[Any] = []

    class InvalidGlm(WindowGlm):
        def select(self, request: Any) -> SelectionResult:
            requests.append(request)
            raise CompletedResponseError(
                "missing decision", ProviderMetadata(self.provider, self.model)
            )

    path, context = _project_with_fake_media(tmp_path, monkeypatch, duration_ms=100_000)
    # Default batching groups these three disagreements into one paid request.
    try:
        pending = run_project(
            context,
            path,
            media_store_factory=FakeMediaStore,
            qwen_asr_factory=LongAsr,
            doubao_asr_factory=LongAsr,
            qwen_correction_factory=LongCorrection,
            kimi_correction_factory=LongKimi,
            glm_selection_factory=InvalidGlm,
        )
        assert len(requests) == 2 and requests[0] == requests[1]
        assert pending["status"] == "needs_review" and pending["review_item_count"] == 3
        final = orchestrator._require(context, pending["run_id"], "edit_resolution")
        assert not final.payload["sealed"] and not final.payload["resolved_edits"]
        assert context.registry.checkpoint(pending["run_id"], "ata_response") is None
    finally:
        context.close()


def test_new_input_invalidates_all_scoped_selection_and_correction_outputs(
    tmp_path: Any,
    monkeypatch: Any,
) -> None:
    from cueflow.orchestrator import _publish_payload_job_input

    context, pending = _long_run(tmp_path, monkeypatch, {1})
    try:
        before = [
            dict(row)
            for row in context.registry.current_artifacts(context.run_id)
            if row["artifact_kind"]
            in {"correction_transcript", "merge_plan", "selection_batch", "selection_result"}
        ]
        assert {row["artifact_kind"] for row in before} == {
            "correction_transcript",
            "merge_plan",
            "selection_batch",
            "selection_result",
        }
        payload = dict(context.current_artifact("job_input").payload)
        payload["user_keywords"] = ["different input"]
        _publish_payload_job_input(context, payload)
        for row in before:
            pointer = context.registry.current_pointer(
                context.run_id, row["artifact_kind"], row["scope_key"]
            )
            assert pointer["is_stale"] == 1
            assert pointer["artifact_id"] == row["artifact_id"]
        # Frozen original checkpoints still retain the original request.
        assert context.registry.checkpoint(pending["run_id"], "merge_plan") is not None
    finally:
        context.close()
