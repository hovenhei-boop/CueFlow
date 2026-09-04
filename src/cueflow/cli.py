from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from cueflow.errors import CueFlowError
from cueflow.job_inputs import ReferenceSpec
from cueflow.orchestrator import (
    correct_project,
    initialize_project,
    project_status,
    resolve_review,
    resume_run,
    retry_invocation,
    run_project,
)
from cueflow.project import ProjectContext


class _ReferenceAction(argparse.Action):
    def __call__(
        self,
        parser: argparse.ArgumentParser,
        namespace: argparse.Namespace,
        values: str | Sequence[Any] | None,
        option_string: str | None = None,
    ) -> None:
        if not isinstance(values, str) or option_string is None:
            raise argparse.ArgumentError(self, "Reference option requires one value")
        kinds = {
            "--pdf-url": "pdf_url",
            "--image-url": "image_url",
            "--text-file": "text_file",
        }
        references = list(getattr(namespace, self.dest, None) or [])
        references.append(ReferenceSpec(kinds[option_string], values))
        setattr(namespace, self.dest, references)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cueflow", description="CueFlow v0.5.2 subtitle generation CLI"
    )
    commands = parser.add_subparsers(dest="command", required=True)

    init = commands.add_parser("init", help="create a new v0.5.2 project")
    init.add_argument("project_dir", type=Path)
    init.add_argument("--name", required=True)

    run = commands.add_parser("run", help="run Base ASR through SRT export")
    run.add_argument("project_dir", type=Path)
    run.add_argument("media", type=Path)
    _add_correction_inputs(run)

    correct = commands.add_parser(
        "correct", help="reuse BaseTranscript with replacement correction inputs"
    )
    correct.add_argument("project_dir", type=Path)
    _add_correction_inputs(correct)

    status = commands.add_parser("status", help="show current project state")
    status.add_argument("project_dir", type=Path)

    retry = commands.add_parser("retry", help="explicitly retry one failed Invocation")
    retry.add_argument("project_dir", type=Path)
    retry.add_argument("invocation_id")

    resume = commands.add_parser("resume", help="resume one run without retrying failed calls")
    resume.add_argument("project_dir", type=Path)
    resume.add_argument("run_id")

    review = commands.add_parser("review", help="resolve every pending correction review item")
    review.add_argument("project_dir", type=Path)
    review.add_argument("decisions", type=Path, help="UTF-8 JSON file containing decisions[]")
    return parser


def _add_correction_inputs(parser: argparse.ArgumentParser) -> None:
    parser.set_defaults(references=[])
    parser.add_argument("--pdf-url", dest="references", action=_ReferenceAction)
    parser.add_argument("--image-url", dest="references", action=_ReferenceAction)
    parser.add_argument("--text-file", dest="references", action=_ReferenceAction)
    parser.add_argument("--keyword", action="append", default=[], dest="keywords")


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        result = _dispatch(args)
    except _CliFailure as exc:
        _write_json(exc.payload, stream=sys.stderr)
        return exc.exit_code
    except KeyboardInterrupt as exc:
        _write_json(_failure_payload(exc), stream=sys.stderr)
        return 130
    except Exception as exc:
        _write_json(_failure_payload(exc), stream=sys.stderr)
        return 2
    _write_json(result)
    return 0


def _dispatch(args: argparse.Namespace) -> dict[str, Any]:
    if args.command == "init":
        context = initialize_project(args.project_dir, args.name)
        try:
            return {
                "status": "created",
                "project_id": context.project_id,
                "project_dir": str(context.root.resolve()),
            }
        finally:
            context.close()
    context = ProjectContext.open(args.project_dir)
    before = context.registry.runs(context.project_id)
    previous_run_id = str(before[-1]["run_id"]) if before else None
    try:
        if args.command == "run":
            return run_project(
                context,
                args.media,
                references=args.references,
                keywords=args.keywords,
            )
        if args.command == "correct":
            return correct_project(context, references=args.references, keywords=args.keywords)
        if args.command == "status":
            return project_status(context)
        if args.command == "retry":
            return retry_invocation(context, args.invocation_id)
        if args.command == "resume":
            return resume_run(context, args.run_id)
        if args.command == "review":
            value = json.loads(args.decisions.read_text(encoding="utf-8-sig"))
            if not isinstance(value, dict) or set(value) != {
                "decisions",
                "run_id",
                "expected_review_queue_artifact_id",
            }:
                raise CueFlowError("review file requires run_id, expected queue ID, and decisions")
            decisions = value["decisions"]
            if not isinstance(decisions, list):
                raise CueFlowError("review decisions must be an array")
            args.run_id = value["run_id"]
            return resolve_review(
                context,
                decisions,
                run_id=value["run_id"],
                expected_review_queue_artifact_id=value["expected_review_queue_artifact_id"],
            )
        raise CueFlowError("unsupported CLI command")
    except BaseException as exc:
        payload = _failure_payload(exc)
        run_id = _failed_run_id(context, args, previous_run_id)
        payload["run_id"] = run_id
        if run_id is not None:
            invocations = context.registry.invocations_for_run(run_id)
            failed = [
                row
                for row in invocations
                if row["status"]
                in {"definitely_not_sent", "delivery_ambiguous", "explicit_failure"}
            ]
            if failed:
                row = failed[-1]
                payload["invocation_id"] = str(row["invocation_id"])
                payload["invocation_status"] = str(row["status"])
                payload["next_actions"] = [
                    {
                        "action": "retry",
                        "invocation_id": str(row["invocation_id"]),
                        "requires_explicit_user_action": True,
                        "automatic_retry": False,
                    },
                    {"action": "status"},
                ]
                if row["status"] == "delivery_ambiguous":
                    payload["delivery_warning"] = (
                        "delivery may have occurred; CueFlow will not retry automatically"
                    )
        raise _CliFailure(payload, 130 if isinstance(exc, KeyboardInterrupt) else 2) from exc
    finally:
        context.close()


def _failed_run_id(
    context: ProjectContext, args: argparse.Namespace, previous_run_id: str | None
) -> str | None:
    if args.command in {"resume", "review"} and getattr(args, "run_id", None):
        try:
            return str(context.registry.run(args.run_id)["run_id"])
        except CueFlowError:
            return None
    if args.command == "retry":
        try:
            return str(context.registry.invocation(args.invocation_id)["run_id"])
        except CueFlowError:
            return None
    if args.command not in {"run", "correct", "review"}:
        return None
    rows = context.registry.runs(context.project_id)
    if rows and str(rows[-1]["run_id"]) != previous_run_id:
        return str(rows[-1]["run_id"])
    return None


def _failure_payload(exc: BaseException) -> dict[str, Any]:
    if isinstance(exc, _CliFailure):
        return exc.payload
    return {
        "status": "failed",
        "error": str(exc) or type(exc).__name__,
        "run_id": None,
        "next_actions": [{"action": "status"}],
    }


class _CliFailure(Exception):
    def __init__(self, payload: dict[str, Any], exit_code: int) -> None:
        super().__init__(str(payload.get("error", "CueFlow command failed")))
        self.payload = payload
        self.exit_code = exit_code


def _write_json(value: dict[str, Any], *, stream: Any | None = None) -> None:
    target = sys.stdout if stream is None else stream
    target.write(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n")


if __name__ == "__main__":
    raise SystemExit(main())
