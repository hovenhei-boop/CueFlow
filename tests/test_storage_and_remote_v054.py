from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from test_orchestrator_v052 import FakeMediaStore

from cueflow.base_asr_provider import QwenFiletransProvider
from cueflow.errors import CancelledError, ContractError, DeliveryAmbiguousError
from cueflow.media_object_store import TosMediaObjectStore
from cueflow.object_storage import bind_object, cleanup_orphans, persist_object
from cueflow.project import RunContext
from cueflow.provider_control import bind_control


def test_tos_forbids_overwrite_and_signs_exact_version_with_wav_content_type(
    tmp_path: Path,
) -> None:
    calls: list[dict[str, Any]] = []

    class Client:
        def put_object_from_file(self, bucket: str, key: str, path: str, **kwargs: Any) -> Any:
            calls.append(kwargs)
            return SimpleNamespace(version_id="version-1")

        def pre_signed_url(self, *args: Any, **kwargs: Any) -> Any:
            calls.append(kwargs)
            return SimpleNamespace(signed_url="https://media.example/file?fresh-signature")

    path = tmp_path / "content-addressed-blob-without-extension"
    path.write_bytes(b"RIFFfixture")
    store = TosMediaObjectStore(
        Client(),
        environment={
            "TOS_ENDPOINT": "endpoint",
            "TOS_REGION": "region",
            "TOS_BUCKET": "bucket",
        },
    )
    store._module = SimpleNamespace(HttpMethodType=SimpleNamespace(Http_Method_Get="GET"))
    first = store.plan_upload(path, "timeline-audio.wav")
    second = store.plan_upload(path, "timeline-audio.wav")
    assert first.object_key != second.object_key
    receipt = store.put(path, first)
    assert calls[0]["forbid_overwrite"] is True
    assert calls[0]["content_type"] in {"audio/x-wav", "audio/wav"}
    assert calls[0]["meta"]["cueflow-sha256"] == first.content_hash
    store.presign_get(receipt)
    assert calls[1]["query"] == {"versionId": "version-1"}


def test_tos_ack_crash_recovers_intent_without_second_put(tmp_path: Path) -> None:
    context = RunContext.create(tmp_path / "run", "fixture")
    path = tmp_path / "input.wav"
    path.write_bytes(b"RIFFfixture")

    class CrashAfterPut(FakeMediaStore):
        calls = 0

        def put(self, path: Path, ref: Any) -> Any:
            type(self).calls += 1
            super().put(path, ref)
            raise DeliveryAmbiguousError("lost acknowledgement")

    try:
        with pytest.raises(DeliveryAmbiguousError):
            persist_object(context, CrashAfterPut(), path, "media")
        row = context.registry.connection.execute("SELECT * FROM object_transfers").fetchone()
        assert row["state"] == "pending"
        receipt = persist_object(context, CrashAfterPut(), path, "media")
        assert receipt.object_key == row["object_key"]
        assert CrashAfterPut.calls == 1
        bind_object(context, receipt)
        assert (
            context.registry.connection.execute("SELECT state FROM object_transfers").fetchone()[0]
            == "bound"
        )
    finally:
        context.close()


def test_orphan_cleanup_requires_owned_ids_and_preserves_bound_objects(tmp_path: Path) -> None:
    context = RunContext.create(tmp_path / "run", "fixture")
    path = tmp_path / "object.txt"
    path.write_text("object")
    store = FakeMediaStore()
    try:
        protected = persist_object(context, store, path, "protected")
        bind_object(context, protected)
        orphan = persist_object(context, store, path, "orphan")
        context.registry.set_run_status(context.run_id, "failed")
        rows = context.registry.connection.execute(
            "SELECT * FROM object_transfers ORDER BY purpose"
        ).fetchall()
        ids = [row["transfer_id"] for row in rows]
        preview = cleanup_orphans(context, store, ids)
        assert [row["action"] for row in preview] == ["would_remove", "protected"]
        assert orphan.object_key in store.objects
        with pytest.raises(ContractError, match="does not belong"):
            cleanup_orphans(context, store, ["foreign-transfer"], dry_run=False)
        cleanup_orphans(context, store, ids, dry_run=False)
        assert orphan.object_key not in store.objects and protected.object_key in store.objects
    finally:
        context.close()


def test_remote_task_receipt_is_committed_before_poll_and_resume_does_not_submit(
    tmp_path: Path,
) -> None:
    context = RunContext.create(tmp_path / "run", "fixture")
    requests: list[str] = []
    invocation = context.registry.create_invocation(
        run_id=context.run_id,
        owner_run_id=context.run_id,
        operation="qwen_asr",
        logical_operation_key="qwen_asr:global",
        provider="dashscope-filetrans",
        requested_model="model",
        idempotency_key="key",
        inputs=[],
    )
    context.registry.set_invocation_status(invocation, "sending")

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request.method)
        assert not context.registry.connection.in_transaction
        if request.method == "POST":
            return httpx.Response(200, json={"output": {"task_id": "remote-task"}})
        assert context.registry.invocation(invocation)["remote_job_id"] == "remote-task"
        return httpx.Response(
            200,
            json={
                "output": {
                    "task_status": "SUCCEEDED",
                    "results": [
                        {
                            "transcripts": [
                                {
                                    "text": "text",
                                    "sentences": [
                                        {"text": "text", "begin_time": 0, "end_time": 1000},
                                    ],
                                }
                            ],
                        }
                    ],
                }
            },
        )

    try:
        with httpx.Client(transport=httpx.MockTransport(respond)) as client:
            provider = QwenFiletransProvider(client, environment={"DASHSCOPE_API_KEY": "fixture"})
            bind_control(context, provider, invocation)
            assert (
                provider.transcribe("https://media.example/wav", user_keywords=[]).source_text
                == "text"
            )
            context.registry.set_invocation_status(invocation, "delivery_ambiguous")
            bind_control(context, provider, invocation)
            provider.transcribe("https://media.example/new-url", user_keywords=[])
        assert requests == ["POST", "GET", "GET"]
    finally:
        context.close()


def test_cancellation_after_remote_submit_retains_task_id(tmp_path: Path) -> None:
    from cueflow.lifecycle import request_cancel

    context = RunContext.create(tmp_path / "run", "fixture")
    invocation = context.registry.create_invocation(
        run_id=context.run_id,
        owner_run_id=context.run_id,
        operation="qwen_asr",
        logical_operation_key="qwen_asr:global",
        provider="dashscope-filetrans",
        requested_model="model",
        idempotency_key="key",
        inputs=[],
    )

    def respond(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        request_cancel(context.registry.path, context.run_id, 1)
        return httpx.Response(200, json={"output": {"task_id": "retained-task"}})

    try:
        with httpx.Client(transport=httpx.MockTransport(respond)) as client:
            provider = QwenFiletransProvider(client, environment={"DASHSCOPE_API_KEY": "fixture"})
            bind_control(context, provider, invocation)
            with pytest.raises(CancelledError):
                provider.transcribe("https://media.example/wav", user_keywords=[])
        assert context.registry.invocation(invocation)["remote_job_id"] == "retained-task"
    finally:
        context.close()
