from __future__ import annotations

import sqlite3
from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass
from typing import Any

from cueflow.errors import CancelledError, IntegrityError
from cueflow.project import RunContext


@dataclass
class ProviderControl:
    checkpoint: Callable[[], None] = lambda: None
    receipt: Callable[[str, str], None] = lambda task_id, status: None
    resume_task_id: str | None = None


def bind_control(context: RunContext, provider: Any, invocation_id: str) -> None:
    if not hasattr(provider, "control"):
        return
    registry, run_id = context.registry, context.run_id
    invocation = registry.invocation(invocation_id)
    number = registry.round_number(run_id)

    def checkpoint() -> None:
        # This callback also runs inside correction threads. Never share SQLite connections.
        with closing(sqlite3.connect(registry.path)) as connection:
            row = connection.execute(
                """SELECT cancellation_requested FROM execution_rounds
                   WHERE run_id=? AND execution_round=?""",
                (run_id, number),
            ).fetchone()
        if row is None:
            raise IntegrityError("missing execution round during provider wait")
        if row[0]:
            raise CancelledError("cancellation requested during provider wait")

    def receipt(task_id: str, status: str) -> None:
        registry.update_remote_job(invocation_id, remote_job_id=task_id, remote_status=status)

    previous = (
        registry.invocation(invocation["retry_of_invocation_id"])
        if invocation["retry_of_invocation_id"]
        else invocation
    )
    known_task = previous["remote_status"] in {"submitted", "pending", "completed"}
    uncertain_submit = (
        previous["remote_status"] == "submitting" and previous["status"] == "delivery_ambiguous"
    )
    task_id = previous["remote_job_id"] if known_task or uncertain_submit else None
    provider.control = ProviderControl(checkpoint, receipt, task_id)
