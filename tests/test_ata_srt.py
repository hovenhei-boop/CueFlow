from __future__ import annotations

import json
from dataclasses import replace
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

from cueflow.asr_contracts import ProviderMetadata
from cueflow.ata_provider import AtaResponse
from cueflow.ata_result import ata_diagnostics, parse_ata_result
from cueflow.cloud_stream import CompletedResponseError
from cueflow.errors import (
    ContractError,
    ExportBlockedError,
    IntegrityError,
    ProviderError,
    SrtSerializationError,
)
from cueflow.export import publish_srt, render_srt
from cueflow.orchestrator import resume_run, run_project
from cueflow.schema import ARTIFACT_KINDS, validate_payload


def _run(context: Any, path: Path, ata: Any, **overrides: Any) -> dict[str, Any]:
    return run_project(
        context, path, **{
            "media_store_factory": FakeMediaStore,
            "qwen_asr_factory": FakeQwenAsr, "doubao_asr_factory": FakeDoubaoAsr,
            "qwen_correction_factory": FakeQwenCorrection,
            "kimi_correction_factory": FakeKimiCorrection, "ata_factory": ata,
            **overrides,
        },
    )


def _ata(raw: bytes) -> Any:
    class ResponseAta(FakeAta):
        calls = 0

        def align(self, url: str, text: str) -> AtaResponse:
            type(self).calls += 1
            return AtaResponse(raw, text, ProviderMetadata(
                self.provider, self.model, self.model, "raw-task", 42
            ))
    return ResponseAta


def _export_args(context: Any, run_id: str) -> dict[str, Any]:
    return {"run_id": run_id, **{
        kind: context.current_artifact(kind)
        for kind in ("timeline_audio", "transcript", "ata_response", "ata_result")
    }}


@pytest.mark.parametrize("utterances", [
    [{"text": " APS-C；USB-C，H.264！F2.8？.NET；node.js，C++ 18-105G。\r\n下一行 ",
      "start_time": 1_200, "end_time": 4_380}],
    [{"text": "first", "start_time": 10_000, "end_time": 12_000},
     {"text": "second", "start_time": 11_800, "end_time": 14_000}],
    [{"text": "later", "start_time": 10_000, "end_time": 12_000},
     {"text": "earlier", "start_time": 1_000, "end_time": 2_000}],
    [{"text": "", "start_time": 0, "end_time": 0}],
    [{"text": "很长的全文" * 2_000, "start_time": 0, "end_time": 9_000_000}],
    [],
])
def test_sentence_result_exports_without_any_quality_gate(
    tmp_path: Path, monkeypatch: Any, utterances: list[dict[str, Any]]
) -> None:
    raw = json.dumps({"code": 0, "utterances": utterances}, ensure_ascii=False).encode()
    path, context = _project_with_fake_media(tmp_path, monkeypatch)
    try:
        result = _run(context, path, _ata(raw))
        normalized = parse_ata_result(raw)
        assert result["status"] == "succeeded"
        assert Path(result["output_path"]).read_bytes() == render_srt(normalized).encode("utf-8")
        assert context.current_artifact("ata_result").payload["utterances"] == normalized
        assert result["diagnostics"] == ata_diagnostics("Qwen3.8 is good.", normalized)
        assert result["diagnostics"]["utterance_count"] == len(utterances)
        assert result["diagnostics"]["text_comparison"]["relation"] == "differs"
        assert "atoms" not in context.current_artifact("transcript").payload
        assert not {"alignment", "subtitle", "qa"} & ARTIFACT_KINDS
        old_fields = {"alignment_artifact_id", "subtitle_artifact_id", "qa_artifact_id"}
        assert not old_fields & result.keys()
    finally:
        context.close()


def test_raw_result_checkpoint_and_invocation_are_atomic(tmp_path: Path, monkeypatch: Any) -> None:
    from cueflow.registry import Registry

    original = Registry._checkpoint_tx

    def interrupted(self: Any, tx: Any, run_id: str, stage: str, *args: Any) -> None:
        original(self, tx, run_id, stage, *args)
        if stage == "ata_response":
            raise RuntimeError("fixture raw commit interrupted")

    raw = b'{"code":0,"utterances":[{"text":"ATA","start_time":0,"end_time":1}]}'
    provider = _ata(raw)
    path, context = _project_with_fake_media(tmp_path, monkeypatch)
    monkeypatch.setattr(Registry, "_checkpoint_tx", interrupted)
    try:
        with pytest.raises(RuntimeError, match="raw commit"):
            _run(context, path, provider)
        run_id = context.registry.run(context.run_id)["run_id"]
        assert context.registry.checkpoint(run_id, "ata_response") is None
        pointer = context.registry.current_pointer(context.run_id, "ata_response", "global")
        assert pointer is None
        invocation = context.registry.invocations_for_run(run_id)[-1]
        assert invocation["status"] == "delivery_ambiguous"
        assert invocation["artifact_id"] is None
        assert invocation["response_id"] == "raw-task"
        assert context.registry.connection.execute(
            "SELECT COUNT(*) FROM artifacts WHERE artifact_kind='ata_response'"
        ).fetchone()[0] == 0
        monkeypatch.setattr(Registry, "_checkpoint_tx", original)
        with pytest.raises(ProviderError, match="explicit retry"):
            resume_run(context, run_id)
        assert provider.calls == 1
    finally:
        context.close()


def test_serializer_is_exact_formatting_not_text_processing() -> None:
    text = "  APS-C，H.264。\r\n.NET！ "
    assert render_srt([
        {"text": text, "start_ms": 1_200, "end_ms": 4_380},
        {"text": "", "start_ms": 4_380, "end_ms": 4_380},
    ]) == f"1\n00:00:01,200 --> 00:00:04,380\n{text}\n\n2\n00:00:04,380 --> 00:00:04,380\n\n"


@pytest.mark.parametrize("malformed", [False, True])
def test_cli_local_ata_failure_never_suggests_an_earlier_paid_retry(
    tmp_path: Path, monkeypatch: Any, capsys: Any, malformed: bool
) -> None:
    from cueflow.cli import main

    class OnceInvalidCorrection(FakeQwenCorrection):
        calls = 0

        def correct(self, request: Any) -> Any:
            type(self).calls += 1
            if type(self).calls == 1:
                raise CompletedResponseError(
                    "fixture invalid JSON", ProviderMetadata(self.provider, self.model),
                    raw_response="invalid", finish_reason="stop",
                )
            return super().correct(request)

    raw = json.dumps({"code": 0, "utterances": [{
        "text": "ATA", **({} if malformed else {"start_time": -1, "end_time": 1})
    }]}).encode()
    provider = _ata(raw)
    path, context = _project_with_fake_media(tmp_path, monkeypatch)
    root = str(context.registry.path.parent.parent)
    try:
        with pytest.raises(ContractError if malformed else SrtSerializationError):
            _run(context, path, provider, qwen_correction_factory=OnceInvalidCorrection)
        run_id = context.registry.run(context.run_id)["run_id"]
        assert OnceInvalidCorrection.calls == 2
        assert any(
            row["status"] == "explicit_failure"
            for row in context.registry.invocations_for_run(run_id)
        )
    finally:
        context.close()
    assert main(["resume", root, run_id]) == 2
    failure = json.loads(capsys.readouterr().err)
    assert failure["run_id"] == run_id and failure["status"] == "failed"
    assert failure["next_actions"] == [{"action": "status"}]
    assert "invocation_id" not in failure
    assert provider.calls == 1


def test_diagnostics_are_exact_receipts_including_signed_delta() -> None:
    items = [
        {"text": "A", "start_ms": -2, "end_ms": 5},
        {"text": "", "start_ms": 4, "end_ms": 8},
        {"text": "B", "start_ms": 9, "end_ms": 8},
        {"text": " ", "start_ms": 9, "end_ms": 9},
    ]
    assert ata_diagnostics("AB  ", items) == {
        "utterance_count": 4,
        "text_comparison": {"relation": "differs", "input_length": 4,
                            "output_length": 3, "length_delta": -1},
        "empty_text_count": 1, "negative_time_count": 1,
        "reversed_interval_count": 1, "zero_duration_count": 1, "overlap_count": 1,
    }
    assert ata_diagnostics("AB ", items)["text_comparison"]["relation"] == "identical"


@pytest.mark.parametrize("start,end", [(-1, 2), (15, 12), (0, -1)])
def test_unserializable_times_are_persisted_but_never_written(
    tmp_path: Path, monkeypatch: Any, start: int, end: int
) -> None:
    raw = json.dumps({"code": 0, "utterances": [
        {"text": "原样", "start_time": start, "end_time": end}
    ]}).encode()
    path, context = _project_with_fake_media(tmp_path, monkeypatch)
    provider = _ata(raw)
    destination = context.root / "attempts" / "1" / "final.srt"
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(b"previous export must survive")
    try:
        with pytest.raises(SrtSerializationError):
            _run(context, path, provider)
        run_id = context.registry.run(context.run_id)["run_id"]
        artifact = context.current_artifact("ata_response")
        blob = artifact.payload["response_blob"]
        assert context.store.blob_path(blob["content_hash"]).read_bytes() == raw
        normalized = context.current_artifact("ata_result")
        assert normalized.payload["utterances"][0]["start_ms"] == start
        assert normalized.payload["utterances"][0]["end_ms"] == end
        validate_payload("ata_result", normalized.payload)
        invocation = context.registry.invocation(artifact.payload["invocation_id"])
        assert invocation["status"] == "succeeded"
        assert context.registry.run(run_id)["status"] == "failed"
        with pytest.raises(SrtSerializationError):
            resume_run(context, run_id)
        assert provider.calls == 1
        assert destination.read_bytes() == b"previous export must survive"
        assert context.registry.current_pointer(context.run_id, "srt_render", "global") is None
    finally:
        context.close()


@pytest.mark.parametrize("malformed", [False, True])
def test_full_raw_over_diagnostic_limit_is_durable_even_if_normalization_fails(
    tmp_path: Path, monkeypatch: Any, malformed: bool
) -> None:
    raw = json.dumps({"code": 0, "unused_raw_field": "证据" * 70_000, "utterances": [
        {"text": "原样", **({} if malformed else {"start_time": 0, "end_time": 100})}
    ]}, ensure_ascii=False, indent=3).encode("utf-8")
    path, context = _project_with_fake_media(tmp_path, monkeypatch)
    provider = _ata(raw)
    try:
        if malformed:
            with pytest.raises(ContractError, match="integer"):
                _run(context, path, provider)
        else:
            _run(context, path, provider)
        artifact = context.current_artifact("ata_response")
        blob = artifact.payload["response_blob"]
        assert blob["byte_length"] == len(raw) > 65_536
        assert context.store.blob_path(blob["content_hash"]).read_bytes() == raw
        row = context.registry.invocation(artifact.payload["invocation_id"])
        assert row["response_id"] == "raw-task" and row["status"] == "succeeded"
        if malformed:
            assert context.registry.checkpoint(row["run_id"], "ata_result") is None
            with pytest.raises(ContractError, match="integer"):
                resume_run(context, row["run_id"])
            assert provider.calls == 1
    finally:
        context.close()


def test_resume_after_result_commit_only_retries_atomic_file_projection(
    tmp_path: Path, monkeypatch: Any
) -> None:
    import cueflow.export as exporter

    path, context = _project_with_fake_media(tmp_path, monkeypatch)
    raw = b'{"code":0,"utterances":[{"text":"ATA","start_time":0,"end_time":1}]}'
    provider = _ata(raw)
    original = exporter._atomic_text_projection

    def interrupted(*args: Any) -> None:
        raise OSError("fixture output unavailable")

    monkeypatch.setattr(exporter, "_atomic_text_projection", interrupted)
    try:
        with pytest.raises(OSError):
            _run(context, path, provider)
        run_id = context.registry.run(context.run_id)["run_id"]
        original_result = context.current_artifact("ata_result").artifact_id
        assert context.registry.checkpoint(run_id, "ata_result") is not None
        monkeypatch.setattr(exporter, "_atomic_text_projection", original)
        result = resume_run(context, run_id, media_store_factory=FakeMediaStore)
        assert result["status"] == "succeeded" and provider.calls == 1
        assert result["ata_result_artifact_id"] == original_result
        expected = b"1\n00:00:00,000 --> 00:00:00,001\nATA\n"
        assert Path(result["output_path"]).read_bytes() == expected
    finally:
        context.close()


@pytest.mark.parametrize(
    "damage", ["stale", "in_memory", "raw_missing", "raw_corrupt", "wrong_run", "invocation"]
)
def test_export_retains_state_and_provenance_checks(
    tmp_path: Path, monkeypatch: Any, damage: str
) -> None:
    path, context = _project_with_fake_media(tmp_path, monkeypatch)
    try:
        result = _run(context, path, FakeAta)
        args = _export_args(context, result["run_id"])
        if damage == "stale":
            context.registry.connection.execute(
                "UPDATE current_pointers SET is_stale=1 WHERE artifact_kind='ata_result'"
            )
        elif damage == "in_memory":
            args["ata_result"] = replace(args["ata_result"], payload={
                **args["ata_result"].payload, "utterances": []
            })
        elif damage == "invocation":
            context.registry.connection.execute(
                "UPDATE invocations SET response_id='another-task' WHERE operation='ata'"
            )
        elif damage.startswith("raw_"):
            blob = args["ata_response"].payload["response_blob"]
            blob_path = context.store.blob_path(blob["content_hash"])
            if damage == "raw_missing":
                blob_path.unlink()
            else:
                blob_path.write_bytes(b"tampered")
        else:
            args["run_id"] = context.registry.create_run()
        with pytest.raises((ExportBlockedError, IntegrityError, OSError)):
            publish_srt(context, **args)
    finally:
        context.close()
