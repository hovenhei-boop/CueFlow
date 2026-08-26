from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from cueflow.errors import CueFlowError, ReferenceRunFailedError
from cueflow.orchestrator import (
    initialize_project,
    project_status,
    retry_invocation,
    run_project,
    set_project_glossary,
)
from cueflow.project import ProjectContext
from cueflow.reference_assets import register_reference_asset, relocate_references
from cueflow.reference_orchestrator import (
    extract_reference,
    reference_status,
    retry_reference_work_item,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="cueflow", description="CueFlow subtitle generation CLI")
    commands = parser.add_subparsers(dest="command", required=True)

    init = commands.add_parser("init", help="create a CueFlow project")
    init.add_argument("project_dir", type=Path)
    init.add_argument("--name", required=True)

    glossary = commands.add_parser("glossary", help="manage the project glossary")
    glossary_commands = glossary.add_subparsers(dest="glossary_command", required=True)
    glossary_set = glossary_commands.add_parser("set", help="replace project glossary terms")
    glossary_set.add_argument("project_dir", type=Path)
    glossary_set.add_argument("glossary_json", type=Path)

    asset = commands.add_parser("asset", help="register an auxiliary SourceAsset")
    asset_commands = asset.add_subparsers(dest="asset_command", required=True)
    asset_add = asset_commands.add_parser("add", help="register an auxiliary file")
    asset_add.add_argument("project_dir", type=Path)
    asset_add.add_argument("file", type=Path)
    asset_add.add_argument("--kind", required=True, choices=("auxiliary",))

    run = commands.add_parser("run", help="run the frozen CueFlow pipeline")
    run.add_argument("project_dir", type=Path)
    run.add_argument("media", type=Path)

    status = commands.add_parser("status", help="show current project state")
    status.add_argument("project_dir", type=Path)

    retry = commands.add_parser("retry", help="explicitly retry a failed Invocation's run")
    retry.add_argument("project_dir", type=Path)
    retry.add_argument("invocation_id")

    reference = commands.add_parser("reference", help="manage Reference Materials")
    reference_commands = reference.add_subparsers(dest="reference_command", required=True)

    reference_add = reference_commands.add_parser("add", help="register a Reference Material")
    reference_add.add_argument("project_dir", type=Path)
    reference_add.add_argument("file", type=Path)

    reference_extract = reference_commands.add_parser(
        "extract", help="create a new Reference extraction Run"
    )
    reference_extract.add_argument("project_dir", type=Path)
    reference_extract.add_argument("reference_asset_id")
    reference_extract.add_argument(
        "--pixel-subtitle-mode", choices=("burned", "none"), default=None
    )

    reference_relocate = reference_commands.add_parser(
        "relocate", help="repair missing Reference locators from one folder"
    )
    reference_relocate.add_argument("project_dir", type=Path)
    reference_relocate.add_argument("folder", type=Path)

    reference_status_parser = reference_commands.add_parser(
        "status", help="show Reference assets and Runs"
    )
    reference_status_parser.add_argument("project_dir", type=Path)
    reference_status_parser.add_argument("reference_asset_id", nargs="?", default=None)

    reference_retry = reference_commands.add_parser(
        "retry", help="retry one failed Reference work item in its original Run"
    )
    reference_retry.add_argument("project_dir", type=Path)
    reference_retry.add_argument("work_item_id")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        result = _dispatch(args)
    except _CliFailure as exc:
        _write_json(exc.payload, stream=sys.stderr)
        return exc.exit_code
    except KeyboardInterrupt as exc:
        _write_json(_generic_failure_payload(exc), stream=sys.stderr)
        return 130
    except Exception as exc:
        _write_json(_generic_failure_payload(exc), stream=sys.stderr)
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
    previous = context.registry.latest_source_run(context.project_id)
    previous_run_id = str(previous["run_id"]) if previous is not None else None
    previous_reference_run_id: str | None = None
    if args.command == "reference" and args.reference_command == "extract":
        prior_runs = context.registry.reference_runs(args.reference_asset_id)
        if prior_runs:
            previous_reference_run_id = str(prior_runs[-1]["run_id"])
    try:
        if args.command == "glossary" and args.glossary_command == "set":
            value = json.loads(args.glossary_json.read_text(encoding="utf-8"))
            if isinstance(value, list):
                terms = value
            elif isinstance(value, dict) and set(value) == {"terms"}:
                terms = value["terms"]
            else:
                raise CueFlowError(
                    "Glossary JSON must be an array or an object containing only terms"
                )
            if not isinstance(terms, list):
                raise CueFlowError("Glossary terms must be an array")
            effective = set_project_glossary(context, terms)
            return {
                "status": "updated",
                "effective_glossary_artifact_id": effective.artifact_id,
                "term_count": len(effective.payload["terms"]),
            }
        if args.command == "asset" and args.asset_command == "add":
            asset = context.register_external_asset(args.file, asset_kind="auxiliary")
            return {"status": "registered", **asset}
        if args.command == "run":
            return run_project(context, args.media)
        if args.command == "status":
            return project_status(context)
        if args.command == "retry":
            return retry_invocation(context, args.invocation_id)
        if args.command == "reference":
            if args.reference_command == "add":
                asset = register_reference_asset(context, args.file)
                return {"status": "registered", **asset}
            if args.reference_command == "extract":
                return extract_reference(
                    context,
                    args.reference_asset_id,
                    pixel_subtitle_mode=args.pixel_subtitle_mode,
                )
            if args.reference_command == "relocate":
                return relocate_references(context, args.folder)
            if args.reference_command == "status":
                return reference_status(context, args.reference_asset_id)
            if args.reference_command == "retry":
                return retry_reference_work_item(context, args.work_item_id)
        raise CueFlowError("unsupported CLI command")
    except KeyboardInterrupt as exc:
        raise _CliFailure(
            _command_failure_payload(
                context, args, exc, previous_run_id, previous_reference_run_id
            ),
            130,
        ) from exc
    except Exception as exc:
        raise _CliFailure(
            _command_failure_payload(
                context, args, exc, previous_run_id, previous_reference_run_id
            ),
            2,
        ) from exc
    finally:
        context.close()


def _write_json(value: MappingLike, *, stream: Any | None = None) -> None:
    target = sys.stdout if stream is None else stream
    target.write(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n")


MappingLike = dict[str, Any]


class _CliFailure(Exception):
    def __init__(self, payload: MappingLike, exit_code: int) -> None:
        super().__init__(str(payload.get("error", "CueFlow command failed")))
        self.payload = payload
        self.exit_code = exit_code


def _command_failure_payload(
    context: ProjectContext,
    args: argparse.Namespace,
    exc: BaseException,
    previous_run_id: str | None,
    previous_reference_run_id: str | None,
) -> MappingLike:
    run_id: str | None = None
    if args.command == "retry":
        try:
            run_id = str(context.registry.invocation(args.invocation_id)["run_id"])
        except CueFlowError:
            pass
    elif args.command == "run":
        latest = context.registry.latest_source_run(context.project_id)
        if latest is not None and str(latest["run_id"]) != previous_run_id:
            run_id = str(latest["run_id"])
    elif args.command == "reference":
        if isinstance(exc, ReferenceRunFailedError):
            run_id = exc.run_id
        elif args.reference_command == "retry":
            try:
                run_id = str(context.registry.reference_work_item(args.work_item_id)["run_id"])
            except CueFlowError:
                pass
        elif args.reference_command == "extract":
            runs = context.registry.reference_runs(args.reference_asset_id)
            if runs and str(runs[-1]["run_id"]) != previous_reference_run_id:
                run_id = str(runs[-1]["run_id"])

    invocation = None
    if run_id is not None:
        invocations = context.registry.invocations_for_run(run_id)
        if invocations and invocations[-1]["status"] in {
            "definitely_not_sent",
            "delivery_ambiguous",
            "explicit_failure",
        }:
            invocation = invocations[-1]

    payload = _generic_failure_payload(exc)
    payload["run_id"] = run_id
    if invocation is not None and args.command != "reference":
        invocation_id = str(invocation["invocation_id"])
        invocation_status = str(invocation["status"])
        payload["invocation_id"] = invocation_id
        payload["invocation_status"] = invocation_status
        payload["next_actions"] = [
            {
                "action": "retry",
                "invocation_id": invocation_id,
                "requires_explicit_user_action": True,
                "automatic_retry": False,
            },
            {"action": "status"},
        ]
        if invocation_status == "delivery_ambiguous":
            payload["delivery_warning"] = (
                "delivery may have occurred; CueFlow will not retry automatically"
            )
    elif args.command == "reference" and run_id is not None:
        failed_items = [
            row
            for row in context.registry.reference_work_items_for_run(run_id)
            if row["status"] in {"failed", "interrupted"}
        ]
        payload["next_actions"] = [
            {
                "action": "reference retry",
                "work_item_id": str(row["work_item_id"]),
                "run_id": run_id,
                "creates_new_run": False,
                "requires_explicit_user_action": True,
            }
            for row in failed_items
        ] + [{"action": "reference status"}]
        if invocation is not None:
            payload["invocation_id"] = str(invocation["invocation_id"])
            payload["invocation_status"] = str(invocation["status"])
            if invocation["status"] == "delivery_ambiguous":
                payload["delivery_warning"] = (
                    "delivery may have occurred; CueFlow will not retry automatically"
                )
    elif args.command == "run":
        payload["next_actions"] = [
            {
                "action": "run",
                "media": str(args.media),
                "creates_new_run": True,
                "automatic_retry": False,
            },
            {"action": "status"},
        ]
    else:
        payload["next_actions"] = [{"action": "status"}]
    return payload


def _generic_failure_payload(exc: BaseException) -> MappingLike:
    return {
        "status": "failed",
        "error": str(exc) or type(exc).__name__,
        "run_id": None,
        "next_actions": [],
    }


if __name__ == "__main__":
    raise SystemExit(main())
