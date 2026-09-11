from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import asdict
from pathlib import Path
from typing import Any

from cueflow.errors import ProviderError
from cueflow.export import _atomic_text_projection
from cueflow.lifecycle import check_cancellation, commit_result, result_snapshot
from cueflow.media_object_store import MediaObjectStore, _hash_file
from cueflow.object_storage import bind_object, persist_object
from cueflow.project import RunContext


def project_result(context: RunContext) -> dict[str, Any]:
    """Repairable local projection; Registry remains authoritative after a crash."""
    result = result_snapshot(context)
    text = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    number = context.registry.round_number(context.run_id)
    _atomic_text_projection(text, context.root / "attempts" / str(number) / "result.json")
    _atomic_text_projection(text, context.root / "result.json")
    return result


def repair_completed_result(context: RunContext) -> dict[str, Any]:
    render = context.current_artifact("srt_render")
    number = context.registry.round_number(context.run_id)
    _atomic_text_projection(
        str(render.payload["text"]), context.root / "attempts" / str(number) / "final.srt"
    )
    return project_result(context)


def publish_result(
    context: RunContext, factory: Callable[[], MediaObjectStore], srt_path: Path
) -> dict[str, Any]:
    result = result_snapshot(context)
    number = context.registry.round_number(context.run_id)
    store = factory()
    try:
        srt = persist_object(context, store, srt_path, f"result:{number}:srt")
        check_cancellation(context)
        result.update(status="succeeded", stage="export", outputs={"srt": asdict(srt)}, error=None)
        checkpoint = context.registry.checkpoint(context.run_id, "timeline_audio")
        if checkpoint:
            result["media_duration_ms"] = context.artifact(checkpoint["artifact_id"]).payload[
                "duration_ms"
            ]
        result_path = srt_path.parent / "result.json"
        _atomic_text_projection(
            json.dumps(result, ensure_ascii=False, indent=2) + "\n", result_path
        )
        manifest = persist_object(context, store, result_path, f"result:{number}:manifest")
        check_cancellation(context)
        # Commit both immutable object receipts before making the current projection visible.
        with context.registry.transaction():
            bind_object(context, srt)
            bind_object(context, manifest)
            commit_result(context, result)
        project_result(context)
        return result
    finally:
        store.close()


def publish_terminal_snapshot(context: RunContext, factory: Callable[[], MediaObjectStore]) -> None:
    result = project_result(context)
    commit_result(context, result)
    number = context.registry.round_number(context.run_id)
    path = context.root / "attempts" / str(number) / "result.json"
    purpose = f"snapshot:{number}:{result['status']}"
    digest, _ = _hash_file(path)
    existing = context.registry.connection.execute(
        """SELECT 1 FROM object_transfers WHERE run_id=? AND purpose=?
           AND content_hash=? AND state='bound' LIMIT 1""",
        (context.run_id, purpose, digest),
    ).fetchone()
    if existing:
        return
    store = None
    try:
        store = factory()
        manifest = persist_object(context, store, path, purpose)
        bind_object(context, manifest)
    except ProviderError:
        # An unavailable object service cannot erase the authoritative failure/round record.
        # The unbound transfer remains visible and can be recovered by exact identity.
        return
    finally:
        if store is not None:
            store.close()
