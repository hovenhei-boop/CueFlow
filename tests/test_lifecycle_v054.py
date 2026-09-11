from __future__ import annotations

import json
import multiprocessing
import sqlite3
import time
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
    _project_with_fake_media,
)

from cueflow.api import Workspace
from cueflow.errors import CancelledError, ContractError, IntegrityError, ProviderError
from cueflow.job_inputs import ReferenceSpec
from cueflow.lifecycle import check_cancellation, request_cancel, result_snapshot
from cueflow.orchestrator import resume_run, retry_run, run_project
from cueflow.project import RunContext, single_writer


def factories(**overrides: Any) -> dict[str, Any]:
    return {
        "media_store_factory": FakeMediaStore,
        "qwen_asr_factory": FakeQwenAsr,
        "doubao_asr_factory": FakeDoubaoAsr,
        "qwen_correction_factory": FakeQwenCorrection,
        "kimi_correction_factory": FakeKimiCorrection,
        "ata_factory": FakeAta,
        **overrides,
    }


def operations(context: Any, number: int) -> list[str]:
    return [
        row["operation"]
        for row in context.registry.invocations_for_run(context.run_id)
        if row["execution_round"] == number
    ]


def test_recovery_exposes_interrupted_and_leaves_other_runs_untouched(tmp_path: Path) -> None:
    context = RunContext.create(tmp_path / "recovery", "fixture")
    registry, run_id = context.registry, context.run_id
    try:
        other = registry.create_run()
        registry.set_run_status(run_id, "running")
        registry.set_run_status(other, "running")
        ids: dict[tuple[str, str], str] = {}
        for target in (run_id, other):
            for status in ("created", "sending"):
                invocation = registry.create_invocation(
                    run_id=target, owner_run_id=target, operation="qwen_asr",
                    logical_operation_key=f"qwen_asr:{status}", provider="fixture",
                    requested_model="fixture", idempotency_key=f"{target}:{status}", inputs=[],
                )
                if status == "sending":
                    registry.set_invocation_status(invocation, status)
                ids[target, status] = invocation
        assert registry.recover_running_source_runs(run_id) == [run_id]
        assert registry.run(run_id)["status"] == "interrupted"
        assert registry.run(other)["status"] == "running"
        rounds = registry.connection.execute(
            "SELECT run_id, status FROM execution_rounds"
        ).fetchall()
        assert {row["run_id"]: row["status"] for row in rounds} == {
            run_id: "interrupted", other: "running",
        }
        for before, after in (
            ("created", "definitely_not_sent"), ("sending", "delivery_ambiguous")
        ):
            assert registry.invocation(ids[run_id, before])["status"] == after
            assert registry.invocation(ids[other, before])["status"] == before
        events = registry.connection.execute(
            "SELECT * FROM progress_events WHERE status='interrupted'"
        ).fetchall()
        assert len(events) == 1 and events[0]["run_id"] == run_id
        assert registry.recover_running_source_runs(run_id) == []
        assert not request_cancel(registry.path, run_id, 1)
        # Human wording, including an absent message, is not a machine protocol.
        for message in ("恢复了异常中断的运行", None):
            registry.set_run_status(run_id, "interrupted", error_message=message)
            result = result_snapshot(context)
            assert result["status"] == "interrupted"
            assert result["error"] == {"code": "interrupted", "message": message}
    finally:
        context.close()


@pytest.mark.parametrize("action", ["resume", "retry"])
def test_interrupted_run_can_resume_or_retry(
    tmp_path: Path, monkeypatch: Any, action: str,
) -> None:
    import cueflow.orchestrator as orchestrator

    original_progress = orchestrator.progress

    def stop_before_ata(context: Any, stage: str) -> None:
        if stage == "ata":
            raise RuntimeError("fixture stop before creating an ATA invocation")
        original_progress(context, stage)

    path, context = _project_with_fake_media(tmp_path, monkeypatch)
    try:
        monkeypatch.setattr(orchestrator, "progress", stop_before_ata)
        with pytest.raises(RuntimeError, match="fixture stop"):
            run_project(context, path, **factories())
        monkeypatch.setattr(orchestrator, "progress", original_progress)
        # Simulate an abandoned running owner with intact upstream checkpoints.
        context.registry.set_run_status(context.run_id, "running")
        if action == "resume":
            context.registry.recover_running_source_runs(context.run_id)
            result = resume_run(context, context.run_id, **factories())
            assert result["execution_round"] == 1
        else:
            result = retry_run(context, context.run_id, **factories())
            assert result["execution_round"] == 2
            assert operations(context, 2) == ["ata"]
        assert result["status"] == "succeeded"
        assert context.registry.connection.execute(
            "SELECT status FROM execution_rounds WHERE run_id=? AND execution_round=1",
            (context.run_id,),
        ).fetchone()[0] == ("succeeded" if action == "resume" else "interrupted")
    finally:
        context.close()


def test_retry_failed_ata_reuses_correction_and_preserves_round_history(
    tmp_path: Path, monkeypatch: Any
) -> None:
    class FailedAta(FakeAta):
        def align(self, *_args: Any) -> Any:
            raise ProviderError("ATA unavailable")

    path, context = _project_with_fake_media(tmp_path, monkeypatch)
    try:
        with pytest.raises(ProviderError):
            run_project(context, path, **factories(ata_factory=FailedAta))
        old = (context.root / "attempts/1/result.json").read_bytes()
        path.unlink()  # The prepared WAV/checkpoints suffice; original input is no longer needed.
        result = retry_run(context, context.run_id, **factories())
        assert result["status"] == "succeeded" and result["execution_round"] == 2
        assert operations(context, 2) == ["ata"]
        assert (context.root / "attempts/1/result.json").read_bytes() == old
        assert (context.root / "attempts/2/final.srt").is_file()
    finally:
        context.close()


def test_retry_failed_kimi_preserves_qwen_only(tmp_path: Path, monkeypatch: Any) -> None:
    class FailedKimi(FakeKimiCorrection):
        def correct(self, request: Any) -> Any:
            raise ProviderError("Kimi unavailable")

    path, context = _project_with_fake_media(tmp_path, monkeypatch)
    try:
        with pytest.raises(ProviderError):
            run_project(context, path, **factories(kimi_correction_factory=FailedKimi))
        qwen = context.registry.checkpoint(context.run_id, "correction_transcript", "qwen")
        retry_run(context, context.run_id, **factories())
        assert operations(context, 2) == ["kimi_correction", "ata"]
        assert (
            context.registry.checkpoint(context.run_id, "correction_transcript", "qwen")[
                "artifact_id"
            ]
            == qwen["artifact_id"]
        )
    finally:
        context.close()


def test_successful_retry_reruns_both_corrections_and_ata(tmp_path: Path, monkeypatch: Any) -> None:
    path, context = _project_with_fake_media(tmp_path, monkeypatch)
    try:
        run_project(context, path, **factories())
        first = (context.root / "attempts/1/final.srt").read_bytes()
        retry_run(context, context.run_id, **factories())
        assert set(operations(context, 2)) == {"qwen_correction", "kimi_correction", "ata"}
        retry_run(context, context.run_id, **factories())
        assert set(operations(context, 3)) == {"qwen_correction", "kimi_correction", "ata"}
        assert (context.root / "attempts/1/final.srt").read_bytes() == first
        assert result_snapshot(context)["execution_round"] == 3
    finally:
        context.close()


def test_office_recovery_expands_effective_refs_and_invalidates_both_arms(
    tmp_path: Path, monkeypatch: Any
) -> None:
    import cueflow.reference_preparation as preparation
    from cueflow.errors import UnsupportedReferenceError

    path, context = _project_with_fake_media(tmp_path, monkeypatch)
    office = tmp_path / "reference.docx"
    office.write_bytes(b"original Office bytes")
    note = tmp_path / "reference.md"
    note.write_text("exact raw reference text", encoding="utf-8")
    conversions = 0

    def convert(source: Path, directory: Path) -> Path:
        nonlocal conversions
        conversions += 1
        assert source.read_bytes() == b"original Office bytes"
        if conversions == 1:
            raise UnsupportedReferenceError("converter timed out")
        target = directory / "canonical.pdf"
        target.write_bytes(b"%PDF-1.7\n1 0 obj<</Type/Catalog>>endobj\n%%EOF\n")
        return target

    monkeypatch.setattr(preparation, "office_to_pdf", convert)
    try:
        first = run_project(
            context,
            path,
            references=[ReferenceSpec("file", str(note)), ReferenceSpec("file", str(office))],
            **factories(),
        )
        assert first["status"] == "succeeded" and len(first["warnings"]) == 1
        original_inputs = list(
            context.registry.connection.execute(
                "SELECT content_hash FROM run_inputs ORDER BY ordinal"
            )
        )
        office.write_bytes(b"caller changed this file; it must never be read again")
        second = retry_run(context, context.run_id, **factories())
        assert second["status"] == "succeeded" and second["warnings"] == []
        assert set(operations(context, 2)) == {"qwen_correction", "kimi_correction", "ata"}
        assert (
            context.current_artifact("job_input").payload["references"][0]["text"]
            == note.read_text()
        )
        assert len(context.current_artifact("job_input").payload["references"]) == 2
        assert (
            list(
                context.registry.connection.execute(
                    "SELECT content_hash FROM run_inputs ORDER BY ordinal"
                )
            )
            == original_inputs
        )
        assert not list((context.root / "temp/inputs").glob("*/*"))
    finally:
        context.close()


def test_still_unavailable_reference_does_not_rerun_successful_models(
    tmp_path: Path, monkeypatch: Any
) -> None:
    path, context = _project_with_fake_media(tmp_path, monkeypatch)
    bad = tmp_path / "bad.pdf"
    bad.write_bytes(b"invalid pdf")
    try:
        result = run_project(
            context, path, references=[ReferenceSpec("file", str(bad))], **factories()
        )
        assert result["warnings"]
        result = retry_run(context, context.run_id, **factories())
        assert result["status"] == "succeeded" and result["warnings"]
        assert operations(context, 2) == []
    finally:
        context.close()


def test_retry_cannot_hide_corrupt_checkpoint_digest(tmp_path: Path, monkeypatch: Any) -> None:
    path, context = _project_with_fake_media(tmp_path, monkeypatch)
    try:
        run_project(context, path, **factories())
        context.registry.connection.execute(
            "UPDATE run_checkpoints SET input_digest='tampered' WHERE stage='correction_transcript'"
        )
        context.registry.connection.commit()
        with pytest.raises(IntegrityError, match="checkpoint identity"):
            retry_run(context, context.run_id, **factories())
        assert context.registry.round_number(context.run_id) == 1
    finally:
        context.close()


def _lock_worker(root: str, run_id: str, ready: Any, release: Any) -> None:
    workspace = Workspace(Path(root))

    @single_writer
    def hold(context: Any) -> None:
        ready.set()
        release.wait(15)

    try:
        hold(workspace.context(run_id))
    finally:
        workspace.close()


def test_two_process_run_locks_and_persistent_cancel_are_independent(tmp_path: Path) -> None:
    workspace = Workspace(tmp_path)
    project_a, project_b = workspace.create_project("A"), workspace.create_project("B")
    first = workspace.registry.create_run(project_a)
    second = workspace.registry.create_run(project_a)
    standalone = workspace.registry.create_run()
    assert len(workspace.list_project_runs(project_a)) == 2
    assert workspace.list_project_runs(project_b) == []
    assert [row["run_id"] for row in workspace.list_standalone_runs()] == [standalone]
    spawn = multiprocessing.get_context("spawn")
    ready, release = spawn.Event(), spawn.Event()
    worker = spawn.Process(target=_lock_worker, args=(str(tmp_path), first, ready, release))
    worker.start()
    try:
        assert ready.wait(10)

        @single_writer
        def touch(context: Any) -> None:
            (context.root / "independent").write_text(context.run_id)

        with pytest.raises(ContractError, match="another CueFlow writer"):
            touch(workspace.context(first))
        touch(workspace.context(second))
        started = time.monotonic()
        assert request_cancel(workspace.registry.path, first, 1)
        assert time.monotonic() - started < 3
        with pytest.raises(CancelledError):
            check_cancellation(workspace.context(first))
        check_cancellation(workspace.context(second))
    finally:
        release.set()
        worker.join(15)
        if worker.is_alive():
            worker.terminate()
            worker.join()
        workspace.close()
    assert worker.exitcode == 0


def test_cancellation_preserves_successful_usage_and_stops_downstream(
    tmp_path: Path, monkeypatch: Any
) -> None:
    path, context = _project_with_fake_media(tmp_path, monkeypatch)

    class CancellingQwen(FakeQwenAsr):
        def transcribe(self, media_url: str, *, user_keywords: Any) -> Any:
            assert not context.registry.connection.in_transaction
            request_cancel(context.registry.path, context.run_id, 1)
            return super().transcribe(media_url, user_keywords=user_keywords)

    try:
        with pytest.raises(CancelledError):
            run_project(context, path, **factories(qwen_asr_factory=CancellingQwen))
        assert context.registry.run(context.run_id)["status"] == "cancelled"
        assert context.registry.checkpoint(context.run_id, "base_asr") is not None
        assert operations(context, 1) == ["media_upload", "qwen_asr"]
        result = json.loads((context.root / "result.json").read_text(encoding="utf-8"))
        assert result["status"] == "cancelled" and result["outputs"]["srt"] is None
        assert result["usage"]["invocations"][-1]["usage"] is None
    finally:
        context.close()


def test_sqlite_failure_is_not_demoted_to_reference_warning(
    tmp_path: Path, monkeypatch: Any
) -> None:
    import cueflow.reference_preparation as preparation

    path, context = _project_with_fake_media(tmp_path, monkeypatch)
    reference = tmp_path / "r.txt"
    reference.write_text("reference")

    def failed_persistence(*args: Any, **kwargs: Any) -> Any:
        raise sqlite3.OperationalError("storage failure")

    monkeypatch.setattr(preparation, "persist_object", failed_persistence)
    try:
        with pytest.raises(sqlite3.OperationalError):
            run_project(
                context, path, references=[ReferenceSpec("file", str(reference))], **factories()
            )
        assert context.registry.run(context.run_id)["status"] == "failed"
    finally:
        context.close()


def _pipeline_worker(
    root: str, source: str, ffmpeg: str, ffprobe: str, barrier: Any, results: Any
) -> None:
    from cueflow.config import RuntimeConfig
    from cueflow.orchestrator import ProviderFactories

    class ConcurrentQwen(FakeQwenAsr):
        def transcribe(self, media_url: str, *, user_keywords: Any) -> Any:
            barrier.wait(timeout=20)
            return super().transcribe(media_url, user_keywords=user_keywords)

    workspace = Workspace(Path(root))
    try:
        result = workspace.run(
            Path(source),
            runtime=RuntimeConfig(ffmpeg, ffprobe),
            factories=ProviderFactories(
                media=FakeMediaStore,
                qwen=ConcurrentQwen,
                doubao=FakeDoubaoAsr,
                qwen_correction=FakeQwenCorrection,
                kimi_correction=FakeKimiCorrection,
                ata=FakeAta,
            ),
        )
        results.put(result)
    finally:
        workspace.close()


def test_two_complete_pipelines_run_concurrently_with_real_media_preparation(
    tmp_path: Path,
) -> None:
    import shutil
    import wave

    tools = Path(__file__).resolve().parents[1] / ".tools/ffmpeg/ffmpeg-9.0.1-essentials_build/bin"
    ffmpeg = (
        str(tools / "ffmpeg.exe") if (tools / "ffmpeg.exe").exists() else shutil.which("ffmpeg")
    )
    ffprobe = (
        str(tools / "ffprobe.exe") if (tools / "ffprobe.exe").exists() else shutil.which("ffprobe")
    )
    if not ffmpeg or not ffprobe:
        pytest.skip("FFmpeg and ffprobe are required for the real media process smoke")
    source = tmp_path / "input.wav"
    with wave.open(str(source), "wb") as stream:
        stream.setnchannels(2)
        stream.setsampwidth(2)
        stream.setframerate(48_000)
        stream.writeframes(b"\0\0\0\0" * 96_000)
    root = tmp_path / "workspace"
    workspace = Workspace(root)
    workspace.close()
    spawn = multiprocessing.get_context("spawn")
    barrier, results = spawn.Barrier(2), spawn.Queue()
    workers = [
        spawn.Process(
            target=_pipeline_worker,
            args=(str(root), str(source), ffmpeg, ffprobe, barrier, results),
        )
        for _ in range(2)
    ]
    try:
        for worker in workers:
            worker.start()
        outcomes = [results.get(timeout=45) for _ in workers]
        assert [outcome["status"] for outcome in outcomes] == ["succeeded", "succeeded"], outcomes
        assert len({outcome["run_id"] for outcome in outcomes}) == 2
        assert len({outcome["outputs"]["srt"]["object_key"] for outcome in outcomes}) == 2
        for outcome in outcomes:
            assert (root / "runs" / outcome["run_id"] / "attempts/1/final.srt").is_file()
            assert outcome["media_duration_ms"] == 2_000
    finally:
        for worker in workers:
            worker.join(10)
            if worker.is_alive():
                worker.terminate()
                worker.join()
        results.close()
    assert all(worker.exitcode == 0 for worker in workers)
