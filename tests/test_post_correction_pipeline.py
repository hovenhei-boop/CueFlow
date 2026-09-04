from __future__ import annotations

from typing import Any

import pytest
from test_orchestrator_v052 import (
    FakeAta,
    FakeDoubaoAsr,
    FakeKimiCorrection,
    FakeKimiNoEdit,
    FakeMediaStore,
    FakeQwenAsr,
    FakeQwenCorrection,
    _project_with_fake_media,
)

from cueflow.asr_contracts import AsrResult, ProviderMetadata, TimedUnit
from cueflow.ata_provider import AlignmentResult
from cueflow.atomizer import atomize
from cueflow.correction_provider import CorrectionResult
from cueflow.edit_resolution import Edit
from cueflow.errors import ContractError, IntegrityError, ProviderError
from cueflow.orchestrator import (
    correct_project,
    resolve_review,
    resume_run,
    retry_invocation,
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
            tuple(
                Edit(sentence, token, token.replace("0", "1"))
                for sentence, token in zip(SENTENCES, ("A0", "B0", "C0"), strict=True)
            ),
            ProviderMetadata(self.provider, self.model),
        )

    def close(self) -> None:
        pass


class WindowGlm:
    provider = "fixture-glm"
    model = "glm-asr-2512"
    calls: list[int] = []
    failures: set[int] = {1}
    next_window: list[int] = [0, 1, 2]

    def transcribe(self, path: Any, *, user_keywords: Any) -> AsrResult:
        assert path.stat().st_size <= 25_000_000
        assert tuple(user_keywords) == ("NVIDIA",)
        index = type(self).next_window.pop(0)
        type(self).calls.append(index)
        if index in self.failures:
            raise TimeoutError("window timeout")
        return AsrResult(
            SENTENCES[index].replace("0", "1"), (), ProviderMetadata(self.provider, self.model)
        )

    def close(self) -> None:
        pass


class TextAta:
    provider = "fixture-ata"
    model = "ata"
    texts: list[str] = []

    def align(self, url: str, text: str) -> AlignmentResult:
        type(self).texts.append(text)
        _, atoms = atomize(text)
        return AlignmentResult(
            tuple(TimedUnit(atom["text"], i * 10, i * 10 + 9) for i, atom in enumerate(atoms)),
            (),
            ProviderMetadata(self.provider, self.model),
        )

    def close(self) -> None:
        pass


def _long_run(tmp_path: Any, monkeypatch: Any, failures: set[int]) -> tuple[Any, dict]:
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
        kimi_correction_factory=FakeKimiNoEdit,
        glm_asr_factory=WindowGlm,
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
            if row["operation"] == "glm_asr" and row["status"] != "succeeded"
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
            if row["operation"] == "glm_asr" and row["status"] != "succeeded"
        )
        WindowGlm.failures, WindowGlm.next_window = set(), [1]
        outcome = retry_invocation(
            context,
            failed["invocation_id"],
            glm_asr_factory=WindowGlm,
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


def test_failed_arm_retry_reuses_success_and_correct_is_a_new_paid_run(
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
        run_id = context.registry.runs(context.project_id)[-1]["run_id"]
        failed = context.registry.invocations_for_run(run_id)[-1]
        retry_invocation(
            context,
            failed["invocation_id"],
            media_store_factory=FakeMediaStore,
            kimi_correction_factory=FakeKimiCorrection,
            ata_factory=FakeAta,
        )
        assert len(FakeQwenCorrection.requests) == len(FakeKimiCorrection.requests) == 1
        new_run = correct_project(
            context,
            media_store_factory=FakeMediaStore,
            qwen_correction_factory=FakeQwenCorrection,
            kimi_correction_factory=FakeKimiCorrection,
            ata_factory=FakeAta,
        )
        assert new_run["run_id"] != run_id
        assert len(FakeQwenCorrection.requests) == len(FakeKimiCorrection.requests) == 2
        operations = [
            row["operation"] for row in context.registry.invocations_for_run(new_run["run_id"])
        ]
        assert operations == ["qwen_correction", "kimi_correction", "ata"]
    finally:
        context.close()


def test_plan_checkpoint_survives_crash_before_glm(tmp_path: Any, monkeypatch: Any) -> None:
    import cueflow.orchestrator as orchestrator

    path, context = _project_with_fake_media(tmp_path, monkeypatch, duration_ms=100_000)
    original = orchestrator._extract_window
    monkeypatch.setattr(
        orchestrator, "_extract_window", lambda *args: (_ for _ in ()).throw(KeyboardInterrupt())
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
                kimi_correction_factory=FakeKimiNoEdit,
                glm_asr_factory=WindowGlm,
            )
        run_id = context.registry.runs(context.project_id)[-1]["run_id"]
        assert len(context.current_artifact("acoustic_window_plan").payload["windows"]) == 3
        assert not WindowGlm.calls
        monkeypatch.setattr(orchestrator, "_extract_window", original)
        result = resume_run(
            context,
            run_id,
            media_store_factory=FakeMediaStore,
            glm_asr_factory=WindowGlm,
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
    path, context = _project_with_fake_media(tmp_path, monkeypatch, duration_ms=100_000)

    class InterruptedGlm(WindowGlm):
        def transcribe(self, path: Any, **kwargs: Any) -> AsrResult:
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
                kimi_correction_factory=FakeKimiNoEdit,
                glm_asr_factory=InterruptedGlm,
            )
        run_id = context.registry.runs(context.project_id)[-1]["run_id"]
        resumed = resume_run(
            context, run_id, media_store_factory=FakeMediaStore, glm_asr_factory=WindowGlm
        )
        assert resumed["status"] == "needs_review"
        assert resumed["review_item_count"] == 1
        assert WindowGlm.calls == [1, 2]
        glm = [
            row
            for row in context.registry.invocations_for_run(run_id)
            if row["operation"] == "glm_asr"
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
        run_id = context.registry.runs(context.project_id)[-1]["run_id"]
        assert context.registry.checkpoint(run_id, "edit_resolution") is None
        assert context.registry.checkpoint(run_id, "review_queue") is None
        monkeypatch.setattr(orchestrator, "_save", original_save)
        result = resume_run(
            context, run_id, media_store_factory=FakeMediaStore, ata_factory=FakeAta
        )
        assert result["status"] == "succeeded"
    finally:
        context.close()


def test_resume_after_ata_commit_does_not_repeat_alignment(tmp_path: Any, monkeypatch: Any) -> None:
    import cueflow.orchestrator as orchestrator

    original_downstream = orchestrator._publish_downstream
    calls: list[str] = []

    class CountingAta(FakeAta):
        def align(self, url: str, text: str) -> AlignmentResult:
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
        run_id = context.registry.runs(context.project_id)[-1]["run_id"]
        assert context.registry.checkpoint(run_id, "alignment") is not None
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
        if kind == "acoustic_window":
            raise IntegrityError("corrupt window blob")
        return original_get(context, run_id, kind, *args)

    failed = next(
        row
        for row in context.registry.invocations_for_run(pending["run_id"])
        if row["operation"] == "glm_asr" and row["status"] != "succeeded"
    )
    monkeypatch.setattr(orchestrator, "_get", corrupt)
    try:
        with pytest.raises(IntegrityError, match="corrupt window"):
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
        run_id = context.registry.runs(context.project_id)[-1]["run_id"]
        assert context.registry.checkpoint(run_id, "base_asr") is None
        assert context.registry.current_pointer(context.project_id, "base_asr", "global") is None
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
