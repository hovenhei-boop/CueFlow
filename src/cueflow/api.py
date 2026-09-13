from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any

from cueflow.artifact_store import ArtifactStore
from cueflow.config import RuntimeConfig
from cueflow.errors import CancelledError, ContractError, TrialExecutionStopped
from cueflow.job_inputs import ReferenceSpec
from cueflow.lifecycle import request_cancel, result_snapshot
from cueflow.orchestrator import (
    ProviderFactories,
    initialize_inputs,
    resume_run,
    retry_invocation,
    retry_run,
)
from cueflow.project import AllowAllExecutionControl, RunContext, RunExecutionControl
from cueflow.publication import project_result
from cueflow.registry import Registry


class Workspace:
    """One local Registry, optional Projects, explicit independent Run directories."""

    def __init__(
        self, root: Path, *, execution_control: RunExecutionControl | None = None
    ) -> None:
        self.root = root.resolve()
        self.registry = Registry(self.root / ".cueflow" / "registry.sqlite3")
        self.execution_control = execution_control or AllowAllExecutionControl()

    def close(self) -> None:
        self.registry.close()

    def create_project(self, name: str) -> str:
        return self.registry.create_project(name)

    def get_project(self, project_id: str) -> dict[str, Any]:
        return dict(self.registry.project(project_id))

    def list_project_runs(self, project_id: str) -> list[dict[str, Any]]:
        self.registry.project(project_id)
        return [dict(row) for row in self.registry.runs(project_id)]

    def list_standalone_runs(self) -> list[dict[str, Any]]:
        return [dict(row) for row in self.registry.list_standalone_runs()]

    def context(self, run_id: str) -> RunContext:
        self.registry.run(run_id)
        root = self.root / "runs" / run_id
        context = RunContext(
            root, self.registry, ArtifactStore(root), run_id, self.execution_control
        )
        if not (root / ".cueflow" / "execution.json").exists():
            context._write_locator()
        return context

    def run(
        self,
        media: Path,
        *,
        project_id: str | None = None,
        references: Sequence[Path] = (),
        keywords: Sequence[str] = (),
        runtime: RuntimeConfig | None = None,
        factories: ProviderFactories | None = None,
    ) -> dict[str, Any]:
        handle = self.create_run(
            media, project_id=project_id, references=references, keywords=keywords
        )
        if handle["status"] != "queued":
            return handle
        return self.execute_run(str(handle["run_id"]), runtime=runtime, factories=factories)

    def create_run(
        self,
        media: Path,
        *,
        project_id: str | None = None,
        references: Sequence[Path] = (),
        keywords: Sequence[str] = (),
    ) -> dict[str, Any]:
        """Freeze input membership and return a queued handle. Scheduling belongs to the caller."""
        context = self.context(self.registry.create_run(project_id))
        try:
            initialize_inputs(
                context, media, [ReferenceSpec("file", str(p)) for p in references], keywords
            )
        except (Exception, KeyboardInterrupt) as exc:
            self.registry.set_run_status(
                context.run_id,
                "cancelled" if isinstance(exc, (CancelledError, KeyboardInterrupt)) else "failed",
                error_message=str(exc) or type(exc).__name__,
            )
        return project_result(context)

    def execute_run(
        self,
        run_id: str,
        *,
        runtime: RuntimeConfig | None = None,
        factories: ProviderFactories | None = None,
    ) -> dict[str, Any]:
        context = self.context(run_id)
        try:
            resume_run(context, run_id, runtime=runtime, **_arguments(factories))
        except (Exception, KeyboardInterrupt) as exc:
            # The executor persists a failure. A lock/contract rejection must not change a live Run.
            if self.registry.run(run_id)["status"] not in {
                "failed", "cancelled", "interrupted"
            }:
                raise
            if isinstance(exc, TrialExecutionStopped):
                return result_snapshot(context)
        return result_snapshot(context)

    def retry_run(
        self,
        run_id: str,
        *,
        runtime: RuntimeConfig | None = None,
        factories: ProviderFactories | None = None,
    ) -> dict[str, Any]:
        context = self.context(run_id)
        retry_run(context, run_id, runtime=runtime, **_arguments(factories))
        return result_snapshot(context)

    def retry_invocation(
        self,
        invocation_id: str,
        *,
        factories: ProviderFactories | None = None,
    ) -> dict[str, Any]:
        invocation = self.registry.invocation(invocation_id)
        context = self.context(str(invocation["run_id"]))
        if invocation["execution_round"] != self.registry.round_number(context.run_id):
            raise ContractError("cannot retry an invocation from an earlier round")
        retry_invocation(context, invocation_id, **_arguments(factories))
        return result_snapshot(context)

    def get_result(self, run_id: str, execution_round: int | None = None) -> dict[str, Any]:
        if execution_round is not None:
            import json

            row = self.registry.connection.execute(
                "SELECT result_json FROM execution_rounds WHERE run_id=? AND execution_round=?",
                (run_id, execution_round),
            ).fetchone()
            if row is None:
                raise ContractError("unknown execution round")
            if row[0] is not None:
                return dict(json.loads(row[0]))
            if self.registry.round_number(run_id) != execution_round:
                raise ContractError("historical round has no committed result")
        return result_snapshot(self.context(run_id))

    def cancel(self, run_id: str, execution_round: int) -> bool:
        return request_cancel(self.registry.path, run_id, execution_round)


def _arguments(factories: ProviderFactories | None) -> dict[str, Any]:
    if factories is None:
        return {}
    return {
        "media_store_factory": factories.media,
        "qwen_asr_factory": factories.qwen,
        "doubao_asr_factory": factories.doubao,
        "glm_selection_factory": factories.glm,
        "qwen_correction_factory": factories.qwen_correction,
        "kimi_correction_factory": factories.kimi_correction,
        "ata_factory": factories.ata,
        "result_media_store_factory": factories.result_media,
    }
