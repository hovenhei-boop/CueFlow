from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from cueflow.api import Workspace
from cueflow.errors import ContractError
from cueflow.orchestrator import resolve_review, resume_run


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="cueflow", description="CueFlow Core 0.5.4")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in (
        "init",
        "project-create",
        "projects",
        "runs",
        "run",
        "status",
        "retry-run",
        "retry-invocation",
        "resume",
        "cancel",
        "review",
        "events",
    ):
        command = commands.add_parser(name)
        command.add_argument("workspace", type=Path)
        if name == "project-create":
            command.add_argument("name")
        if name in {"run", "runs"}:
            command.add_argument("--project")
        if name == "run":
            command.add_argument("media", type=Path)
            command.add_argument("--reference", action="append", type=Path, default=[])
            command.add_argument("--keyword", action="append", default=[])
        if name in {"status", "retry-run", "resume", "cancel", "review", "events"}:
            command.add_argument("run_id")
        if name == "retry-invocation":
            command.add_argument("invocation_id")
        if name == "cancel":
            command.add_argument("--round", type=int, required=True)
        if name == "review":
            command.add_argument("decisions", type=Path)
        if name == "events":
            command.add_argument("--after", type=int, default=0)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    workspace: Workspace | None = None
    try:
        workspace = Workspace(args.workspace)
        result = _dispatch(workspace, args)
    except (Exception, KeyboardInterrupt) as exc:
        result = {
            "contract_version": "1.0",
            "status": "failed",
            "error": {"code": type(exc).__name__, "message": str(exc)},
            "next_actions": [{"action": "status"}],
        }
        if workspace and getattr(args, "run_id", None):
            result["run_id"] = args.run_id
        _write_json(result, stream=sys.stderr)
        return 130 if isinstance(exc, KeyboardInterrupt) else 2
    finally:
        if workspace:
            workspace.close()
    _write_json(result)
    return 2 if result.get("status") in {"failed", "cancelled"} else 0


def _dispatch(workspace: Workspace, args: argparse.Namespace) -> dict[str, Any]:
    if args.command == "init":
        return {"workspace": str(workspace.root)}
    if args.command == "project-create":
        return workspace.get_project(workspace.create_project(args.name))
    if args.command == "projects":
        return {"projects": [dict(row) for row in workspace.registry.projects()]}
    if args.command == "runs":
        return {
            "runs": workspace.list_project_runs(args.project)
            if args.project
            else workspace.list_standalone_runs()
        }
    if args.command == "run":
        return workspace.run(
            args.media, project_id=args.project, references=args.reference, keywords=args.keyword
        )
    if args.command == "retry-run":
        return workspace.retry_run(args.run_id)
    if args.command == "retry-invocation":
        return workspace.retry_invocation(args.invocation_id)
    if args.command == "status":
        return workspace.get_result(args.run_id)
    if args.command == "cancel":
        return {
            "run_id": args.run_id,
            "execution_round": args.round,
            "cancellation_requested": workspace.cancel(args.run_id, args.round),
        }
    if args.command == "events":
        workspace.registry.run(args.run_id)
        return {
            "events": [
                dict(row)
                for row in workspace.registry.connection.execute(
                    "SELECT * FROM progress_events WHERE run_id=? AND event_id>? ORDER BY event_id",
                    (args.run_id, args.after),
                )
            ]
        }
    context = workspace.context(args.run_id)
    if args.command == "resume":
        resume_run(context, args.run_id)
        return workspace.get_result(args.run_id)
    if args.command == "review":
        value = json.loads(args.decisions.read_text(encoding="utf-8-sig"))
        if value.get("run_id") != args.run_id:
            raise ContractError("review Run identity mismatch")
        resolve_review(
            context,
            value["decisions"],
            run_id=args.run_id,
            expected_review_queue_artifact_id=value["expected_review_queue_artifact_id"],
        )
        return workspace.get_result(args.run_id)
    raise ContractError("unknown command")


def _write_json(value: dict[str, Any], *, stream: Any | None = None) -> None:
    target = sys.stdout if stream is None else stream
    target.write(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
