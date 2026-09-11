from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from cueflow.errors import CancelledError, ContractError, IntegrityError
from cueflow.project import RunContext
from cueflow.registry import Registry
from cueflow.schema import utc_now

# Edges describe execution dependencies, without changing any algorithm.
DOWNSTREAM: dict[str, set[str]] = {
    "job_input": {"correction_transcript"},
    "media_probe": {"timeline_audio"},
    "timeline_audio": {"media_object"},
    "media_object": {"base_asr", "peer_asr", "ata_response"},
    "base_asr": {"asr_comparison"},
    "peer_asr": {"asr_comparison"},
    "asr_comparison": {"correction_transcript"},
    "correction_transcript": {"merge_plan"},
    "merge_plan": {"selection_batch", "edit_resolution"},
    "selection_batch": {"selection_result"},
    "selection_result": {"edit_resolution"},
    "edit_resolution": {"review_queue", "review_resolution", "transcript"},
    "transcript": {"ata_response"},
    "ata_response": {"ata_result"},
    "ata_result": {"srt_render"},
}
OPERATIONS = {
    "media_upload": "media_object",
    "qwen_asr": "base_asr",
    "doubao_asr": "peer_asr",
    "qwen_correction": "correction_transcript",
    "kimi_correction": "correction_transcript",
    "glm_selection": "selection_result",
    "ata": "ata_response",
}


def descendants(roots: set[str]) -> set[str]:
    result: set[str] = set()
    pending = list(roots)
    while pending:
        for stage in DOWNSTREAM.get(pending.pop(), set()):
            if stage not in result:
                result.add(stage)
                pending.append(stage)
    return result


def check_cancellation(context: RunContext) -> None:
    row = context.registry.connection.execute(
        """SELECT cancellation_requested FROM execution_rounds
           WHERE run_id=? AND execution_round=?""",
        (context.run_id, context.registry.round_number(context.run_id)),
    ).fetchone()
    if row is None:
        raise IntegrityError("missing execution round")
    if row[0]:
        raise CancelledError("cancellation requested; submitted provider work may still be billed")


def request_cancel(database: Path, run_id: str, execution_round: int) -> bool:
    # Independent connection, no Run OS lock and no network I/O in the transaction.
    registry = Registry(database)
    try:
        with registry.transaction() as tx:
            row = tx.execute(
                "SELECT status FROM execution_rounds WHERE run_id=? AND execution_round=?",
                (run_id, execution_round),
            ).fetchone()
            if row is None:
                raise ContractError("unknown execution round")
            if (
                row[0] in {"succeeded", "failed", "cancelled", "interrupted"}
                or registry.round_number(run_id) != execution_round
            ):
                return False
            tx.execute(
                """UPDATE execution_rounds SET cancellation_requested=1, updated_at=?
                   WHERE run_id=? AND execution_round=?""",
                (utc_now(), run_id, execution_round),
            )
        return True
    finally:
        registry.close()


def progress(context: RunContext, stage: str) -> None:
    check_cancellation(context)
    registry, run_id = context.registry, context.run_id
    number, now = registry.round_number(run_id), utc_now()
    with registry.transaction() as tx:
        tx.execute(
            "UPDATE execution_rounds SET stage=?, updated_at=? "
            "WHERE run_id=? AND execution_round=?",
            (stage, now, run_id, number),
        )
        tx.execute(
            """INSERT INTO progress_events(run_id, execution_round, status, stage, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            (run_id, number, registry.run(run_id)["status"], stage, now),
        )


def invalidate(context: RunContext, roots: set[str]) -> None:
    registry, run_id = context.registry, context.run_id
    stages = roots | descendants(roots)
    with registry.transaction() as tx:
        for stage in stages:
            tx.execute(
                "DELETE FROM run_checkpoints WHERE run_id=? AND execution_round=? AND stage=?",
                (run_id, registry.round_number(run_id, stage), stage),
            )
            tx.execute(
                "UPDATE current_pointers SET is_stale=1 WHERE owner_run_id=? AND artifact_kind=?",
                (run_id, stage),
            )


def begin_retry(context: RunContext) -> int:
    from cueflow.run_runtime import _checkpoint_args, _get

    registry, run_id = context.registry, context.run_id
    row = registry.run(run_id)
    if row["source_asset_id"] is None:
        raise ContractError("Run intake is incomplete; resume intake before retry")
    if row["status"] in {"queued", "running"}:
        raise ContractError("cannot retry an active round")
    old = int(row["execution_round"])
    prior = registry.connection.execute(
        "SELECT * FROM run_checkpoints WHERE run_id=? AND execution_round=?",
        (run_id, old),
    ).fetchall()
    # Validate, do not erase input_digest checks while carrying results forward.
    for checkpoint in prior:
        _get(context, run_id, checkpoint["stage"], checkpoint["scope_key"])
    latest: dict[str, sqlite3.Row] = {}
    for invocation in registry.invocations_for_run(run_id):
        if invocation["execution_round"] == old:
            latest[str(invocation["logical_operation_key"])] = invocation
    failures = [item for item in latest.values() if item["status"] != "succeeded"]
    failed_refs = registry.connection.execute(
        """SELECT 1 FROM reference_preparations
           WHERE run_id=? AND execution_round=? AND status='unavailable' LIMIT 1""",
        (run_id, old),
    ).fetchone()
    roots = {OPERATIONS[str(item["operation"])] for item in failures}
    # Only the failed correction arm is removed; its sibling remains reusable.
    excluded = descendants(roots)
    failed_keys = {
        (OPERATIONS[str(item["operation"])], str(item["logical_operation_key"]).split(":", 1)[1])
        for item in failures
    }
    if not failures and not failed_refs and row["status"] == "succeeded":
        excluded |= {"correction_transcript"} | descendants({"correction_transcript"})
    new, now = old + 1, utc_now()
    with registry.transaction() as tx:
        tx.execute(
            """INSERT INTO execution_rounds(run_id, execution_round, status, created_at, updated_at)
               VALUES (?, ?, 'queued', ?, ?)""",
            (run_id, new, now, now),
        )
        tx.execute(
            """UPDATE runs SET execution_round=?, status='queued', error_message=NULL,
               updated_at=? WHERE run_id=?""",
            (new, now, run_id),
        )
        for checkpoint in prior:
            stage, scope = str(checkpoint["stage"]), str(checkpoint["scope_key"])
            if stage in excluded or (stage, scope) in failed_keys:
                continue
            args = _checkpoint_args(context, run_id, stage, scope)
            registry._checkpoint_tx(tx, *args, str(checkpoint["artifact_id"]))
        # A current pointer can never make an excluded artifact look like this round's result.
        registry._stale(tx, run_id, [(kind, None) for kind in excluded])
    return new


def result_snapshot(context: RunContext, *, committed: bool = True) -> dict[str, Any]:
    registry, run_id = context.registry, context.run_id
    run = registry.run(run_id)
    number = registry.round_number(run_id)
    round_row = registry.connection.execute(
        "SELECT * FROM execution_rounds WHERE run_id=? AND execution_round=?",
        (run_id, number),
    ).fetchone()
    warnings = [
        {
            "reference_ordinal": row["ordinal"],
            "code": "reference_unavailable",
            "message": row["error_message"],
            "retryable": True,
        }
        for row in registry.connection.execute(
            """SELECT * FROM reference_preparations WHERE run_id=? AND execution_round=?
               AND status='unavailable' ORDER BY ordinal""",
            (run_id, number),
        )
    ]
    calls = [
        {
            "invocation_id": item["invocation_id"],
            "execution_round": item["execution_round"],
            "status": item["status"],
            "usage": json.loads(item["usage_json"]) if item["usage_json"] is not None else None,
        }
        for item in registry.invocations_for_run(run_id)
    ]
    result: dict[str, Any] = {
        "contract_version": "1.0",
        "run_id": run_id,
        "project_id": run["project_id"],
        "execution_round": number,
        "status": run["status"],
        "stage": round_row["stage"],
        "progress": None,
        "cancellation_requested": bool(round_row["cancellation_requested"]),
        "outputs": {"srt": None},
        "warnings": warnings,
        "usage": {"invocations": calls},
        "remote_tasks": [
            {
                "invocation_id": item["invocation_id"],
                "provider": item["provider"],
                "task_id": item["remote_job_id"],
                "status": item["remote_status"],
            }
            for item in registry.invocations_for_run(run_id)
            if item["remote_job_id"]
        ],
        "error": {
            "code": "interrupted" if run["status"] == "interrupted" else "run_failed",
            "message": run["error_message"],
        }
        if run["error_message"] or run["status"] == "interrupted"
        else None,
    }
    if committed and round_row["result_json"] is not None:
        saved = json.loads(round_row["result_json"])
        if saved["status"] == run["status"]:
            return dict(saved)
    return result


def commit_result(context: RunContext, result: Mapping[str, Any]) -> None:
    registry, run_id = context.registry, context.run_id
    with registry.transaction() as tx:
        if result["status"] == "succeeded":
            check_cancellation(context)
        tx.execute(
            """UPDATE execution_rounds SET result_json=?, status=?, updated_at=?
               WHERE run_id=? AND execution_round=?""",
            (
                json.dumps(dict(result), ensure_ascii=False),
                result["status"],
                utc_now(),
                run_id,
                registry.round_number(run_id),
            ),
        )
        tx.execute("UPDATE runs SET status=? WHERE run_id=?", (result["status"], run_id))
        tx.execute(
            """INSERT INTO progress_events(run_id, execution_round, status, stage, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            (run_id, registry.round_number(run_id), result["status"], result["stage"], utc_now()),
        )
