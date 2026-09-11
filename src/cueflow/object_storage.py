from __future__ import annotations

import json
import uuid
from collections.abc import Sequence
from dataclasses import asdict
from pathlib import Path

from cueflow.errors import ContractError
from cueflow.media_object_store import MediaObjectRef, MediaObjectStore, _hash_file
from cueflow.project import RunContext, single_writer
from cueflow.schema import utc_now


def persist_object(
    context: RunContext,
    store: MediaObjectStore,
    path: Path,
    purpose: str,
    *,
    object_name: str | None = None,
) -> MediaObjectRef:
    """Durable intent precedes PUT. Resume verifies the same key before any new upload."""
    registry = context.registry
    digest, size = _hash_file(path, allow_empty=True)
    number = registry.round_number(context.run_id)
    previous = registry.connection.execute(
        """SELECT * FROM object_transfers WHERE run_id=? AND purpose=? AND content_hash=?
           AND state != 'removed' ORDER BY created_at DESC LIMIT 1""",
        (context.run_id, purpose, digest),
    ).fetchone()
    if previous is None:
        ref = store.plan_upload(path, object_name or path.name)
        transfer_id = uuid.uuid4().hex
        with registry.transaction() as tx:
            tx.execute(
                "INSERT INTO object_transfers VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)",
                (
                    transfer_id,
                    context.run_id,
                    number,
                    purpose,
                    digest,
                    size,
                    ref.bucket,
                    ref.object_key,
                    json.dumps(asdict(ref)),
                    utc_now(),
                ),
            )
    else:
        transfer_id = str(previous["transfer_id"])
        ref = MediaObjectRef(**json.loads(previous["object_json"]))
        if previous["state"] in {"persisted", "bound"}:
            return ref
    # A failed HEAD never means absent. Only a definite 404 permits a PUT.
    remote = store.head(ref)
    if remote is None:
        remote = store.put(path, ref)
    with registry.transaction() as tx:
        tx.execute(
            "UPDATE object_transfers SET state='persisted', object_json=? WHERE transfer_id=?",
            (json.dumps(asdict(remote)), transfer_id),
        )
    return remote


def bind_object(context: RunContext, ref: MediaObjectRef) -> None:
    with context.registry.transaction() as tx:
        tx.execute(
            "UPDATE object_transfers SET state='bound' WHERE run_id=? AND object_key=?",
            (context.run_id, ref.object_key),
        )


@single_writer
def cleanup_orphans(
    context: RunContext,
    store: MediaObjectStore,
    transfer_ids: Sequence[str],
    *,
    dry_run: bool = True,
) -> list[dict[str, str]]:
    """Explicit owned transfer IDs only. No bucket listing or automatic destructive sweep."""
    registry, run_id = context.registry, context.run_id
    if registry.run(run_id)["status"] in {"queued", "running", "needs_review"}:
        raise ContractError("active Run transfers cannot be cleaned")
    results: list[dict[str, str]] = []
    for transfer_id in transfer_ids:
        row = registry.connection.execute(
            "SELECT * FROM object_transfers WHERE run_id=? AND transfer_id=?",
            (run_id, transfer_id),
        ).fetchone()
        if row is None:
            raise ContractError("transfer does not belong to the requested Run")
        ref = MediaObjectRef(**json.loads(row["object_json"]))
        protected = row["state"] == "bound" or _referenced(context, ref.object_key)
        if protected:
            results.append({"transfer_id": transfer_id, "action": "protected"})
            continue
        if not dry_run and row["state"] != "removed":
            if store.head(ref) is not None:
                store.delete(ref)
            with registry.transaction() as tx:
                tx.execute(
                    "UPDATE object_transfers SET state='removed' WHERE transfer_id=?",
                    (transfer_id,),
                )
        results.append(
            {"transfer_id": transfer_id, "action": "would_remove" if dry_run else "removed"}
        )
    return results


def _referenced(context: RunContext, object_key: str) -> bool:
    registry, run_id = context.registry, context.run_id
    documents: list[str] = []
    for table, column in (
        ("run_inputs", "object_json"),
        ("reference_preparations", "prepared_json"),
        ("execution_rounds", "result_json"),
    ):
        documents.extend(
            row[0]
            for row in registry.connection.execute(
                f"SELECT {column} FROM {table} WHERE run_id=? AND {column} IS NOT NULL",
                (run_id,),
            )
        )
    for row in registry.connection.execute(
        "SELECT artifact_id FROM artifacts WHERE owner_run_id=? AND artifact_kind='media_object'",
        (run_id,),
    ):
        if context.artifact(row[0]).payload["object_key"] == object_key:
            return True

    def contains(value: object) -> bool:
        if isinstance(value, dict):
            return value.get("object_key") == object_key or any(contains(v) for v in value.values())
        if isinstance(value, list):
            return any(contains(v) for v in value)
        return False

    return any(contains(json.loads(document)) for document in documents)
