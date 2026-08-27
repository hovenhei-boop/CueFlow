from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from cueflow.errors import (
    CueFlowError,
    LexiconRunFailedError,
    ReferenceRunFailedError,
    SuppressionConflictError,
)
from cueflow.lexicon import (
    BLACKLIST_DURATION_DAYS,
    BlacklistKind,
    add_blacklist,
    add_entry,
    block_entry,
    edit_entry,
    list_blacklist,
    list_entries,
    list_suggestions,
    remove_entry,
    review_candidate,
    set_entry_enabled,
    unblock_blacklist,
    update_blacklist,
)
from cueflow.lexicon_orchestrator import retry_suggestion_work_item, suggestion_status
from cueflow.lexicon_packs import OfficialPackStore
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
from cueflow.registry import migrate_registry


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="cueflow", description="CueFlow subtitle generation CLI")
    commands = parser.add_subparsers(dest="command", required=True)

    init = commands.add_parser("init", help="create a CueFlow project")
    init.add_argument("project_dir", type=Path)
    init.add_argument("--name", required=True)

    migrate = commands.add_parser("migrate", help="explicitly migrate a CueFlow project")
    migrate.add_argument("project_dir", type=Path)

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

    lexicon = commands.add_parser("lexicon", help="manage Suggested Terms and Lexicons")
    lexicon_commands = lexicon.add_subparsers(dest="lexicon_command", required=True)

    suggestions = lexicon_commands.add_parser("suggestions", help="review Suggested Terms")
    suggestion_commands = suggestions.add_subparsers(
        dest="suggestions_command", required=True
    )
    suggestions_list = suggestion_commands.add_parser("list", help="list pending terms")
    suggestions_list.add_argument("project_dir", type=Path)
    suggestions_status = suggestion_commands.add_parser(
        "status", help="show automatic term-discovery jobs"
    )
    suggestions_status.add_argument("project_dir", type=Path)
    suggestions_retry = suggestion_commands.add_parser(
        "retry", help="retry one failed automatic term-discovery batch"
    )
    suggestions_retry.add_argument("project_dir", type=Path)
    suggestions_retry.add_argument("work_item_id")
    suggestions_review = suggestion_commands.add_parser(
        "review", help="accept, edit, dismiss, or block a Suggested Term"
    )
    suggestions_review.add_argument("project_dir", type=Path)
    suggestions_review.add_argument("candidate_id")
    suggestions_review.add_argument(
        "--action",
        required=True,
        choices=(
            "accept",
            "edit_accept",
            "dismiss",
            "block_temporary",
            "block_permanent",
        ),
    )
    suggestions_review.add_argument("--expected-revision", required=True, type=int)
    suggestions_review.add_argument("--term", dest="edited_term")
    suggestions_review.add_argument(
        "--category", choices=("proper_noun", "noun_or_term", "verb", "other")
    )
    suggestions_review.add_argument("--proper-noun-subtype")
    suggestions_review.add_argument("--days", type=int, choices=BLACKLIST_DURATION_DAYS)
    _add_blacklist_policy(suggestions_review)

    entry = lexicon_commands.add_parser("entry", help="manage the Project Lexicon")
    entry_commands = entry.add_subparsers(dest="entry_command", required=True)
    entry_list = entry_commands.add_parser("list", help="list Project Lexicon entries")
    entry_list.add_argument("project_dir", type=Path)
    entry_list.add_argument("--include-removed", action="store_true")
    entry_add = entry_commands.add_parser("add", help="add a Project Lexicon entry")
    entry_add.add_argument("project_dir", type=Path)
    entry_add.add_argument("term")
    entry_add.add_argument(
        "--category", required=True, choices=("proper_noun", "noun_or_term", "verb", "other")
    )
    entry_add.add_argument("--proper-noun-subtype")
    _add_blacklist_policy(entry_add)
    entry_edit = entry_commands.add_parser("edit", help="edit a Project Lexicon entry")
    entry_edit.add_argument("project_dir", type=Path)
    entry_edit.add_argument("entry_id")
    entry_edit.add_argument("term")
    entry_edit.add_argument(
        "--category", required=True, choices=("proper_noun", "noun_or_term", "verb", "other")
    )
    entry_edit.add_argument("--proper-noun-subtype")
    entry_edit.add_argument("--expected-revision", required=True, type=int)
    _add_blacklist_policy(entry_edit)
    for command_name in ("enable", "disable", "remove"):
        command = entry_commands.add_parser(command_name)
        command.add_argument("project_dir", type=Path)
        command.add_argument("entry_id")
        command.add_argument("--expected-revision", required=True, type=int)

    entry_block = entry_commands.add_parser(
        "block", help="remove and block a Project Lexicon entry atomically"
    )
    entry_block.add_argument("project_dir", type=Path)
    entry_block.add_argument("entry_id")
    entry_block.add_argument("--expected-revision", required=True, type=int)
    _add_blacklist_kind(entry_block)

    blacklist = lexicon_commands.add_parser(
        "blacklist", help="suppress exact Reference suggestions"
    )
    blacklist_commands = blacklist.add_subparsers(dest="blacklist_command", required=True)
    blacklist_list = blacklist_commands.add_parser("list")
    blacklist_list.add_argument("project_dir", type=Path)
    blacklist_list.add_argument(
        "--kind", choices=("all", "temporary", "permanent"), default="all"
    )
    blacklist_add = blacklist_commands.add_parser("add")
    blacklist_add.add_argument("project_dir", type=Path)
    blacklist_add.add_argument("term")
    _add_blacklist_kind(blacklist_add)
    blacklist_update = blacklist_commands.add_parser("update")
    blacklist_update.add_argument("project_dir", type=Path)
    blacklist_update.add_argument("blacklist_id")
    blacklist_update.add_argument("--expected-revision", required=True, type=int)
    _add_blacklist_kind(blacklist_update)
    blacklist_unblock = blacklist_commands.add_parser("unblock")
    blacklist_unblock.add_argument("project_dir", type=Path)
    blacklist_unblock.add_argument("blacklist_id")
    blacklist_unblock.add_argument("--expected-revision", required=True, type=int)

    pack = lexicon_commands.add_parser("pack", help="manage shared Official Lexicon Packs")
    pack_commands = pack.add_subparsers(dest="pack_command", required=True)
    pack_list = pack_commands.add_parser("list")
    pack_list.add_argument("--catalog", type=Path)
    pack_setup = pack_commands.add_parser("setup")
    pack_setup.add_argument("catalog", type=Path)
    pack_setup.add_argument("--domain", action="append", dest="domains")
    pack_install = pack_commands.add_parser("install")
    pack_install.add_argument("--catalog", type=Path)
    pack_install.add_argument("--pack-id", action="append", dest="pack_ids")
    pack_install.add_argument("--domain", action="append", dest="domains")
    pack_uninstall = pack_commands.add_parser("uninstall")
    pack_uninstall.add_argument("pack_ids", nargs="+")
    pack_update = pack_commands.add_parser("update")
    pack_update.add_argument("--catalog", type=Path)
    pack_commands.add_parser("status")
    pack_repair = pack_commands.add_parser("repair")
    pack_repair.add_argument("--catalog", type=Path)
    return parser


def _add_blacklist_policy(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--blacklist-policy",
        default="prompt",
        choices=("prompt", "unblock_and_add", "cancel"),
    )


def _add_blacklist_kind(parser: argparse.ArgumentParser) -> None:
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--temporary", type=int, choices=BLACKLIST_DURATION_DAYS)
    group.add_argument("--permanent", action="store_true")


def _blacklist_kind(args: argparse.Namespace) -> tuple[BlacklistKind, int | None]:
    if args.permanent:
        return "permanent", None
    return "temporary", int(args.temporary)


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
    if args.command == "migrate":
        result = migrate_registry(args.project_dir / ".cueflow" / "registry.sqlite3")
        return {**result, "project_dir": str(args.project_dir.resolve())}
    if args.command == "lexicon" and args.lexicon_command == "pack":
        return _dispatch_pack(args, OfficialPackStore())
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
        if args.command == "lexicon":
            if args.lexicon_command == "suggestions":
                if args.suggestions_command == "list":
                    return {"suggestions": list_suggestions(context)}
                if args.suggestions_command == "status":
                    return suggestion_status(context)
                if args.suggestions_command == "retry":
                    return retry_suggestion_work_item(context, args.work_item_id)
                if args.suggestions_command == "review":
                    return review_candidate(
                        context,
                        args.candidate_id,
                        args.action,
                        edited_term=args.edited_term,
                        edited_category=args.category,
                        edited_subtype=args.proper_noun_subtype,
                        expected_revision=args.expected_revision,
                        blacklist_days=args.days,
                        blacklist_policy=args.blacklist_policy,
                    )
            if args.lexicon_command == "entry":
                if args.entry_command == "list":
                    return {
                        "entries": list_entries(
                            context, include_removed=args.include_removed
                        )
                    }
                if args.entry_command == "add":
                    return add_entry(
                        context,
                        args.term,
                        category=args.category,
                        proper_noun_subtype=args.proper_noun_subtype,
                        blacklist_policy=args.blacklist_policy,
                    )
                if args.entry_command == "edit":
                    return edit_entry(
                        context,
                        args.entry_id,
                        term=args.term,
                        category=args.category,
                        proper_noun_subtype=args.proper_noun_subtype,
                        expected_revision=args.expected_revision,
                        blacklist_policy=args.blacklist_policy,
                    )
                if args.entry_command in {"enable", "disable"}:
                    return set_entry_enabled(
                        context,
                        args.entry_id,
                        enabled=args.entry_command == "enable",
                        expected_revision=args.expected_revision,
                    )
                if args.entry_command == "remove":
                    return remove_entry(
                        context,
                        args.entry_id,
                        expected_revision=args.expected_revision,
                    )
                if args.entry_command == "block":
                    kind, days = _blacklist_kind(args)
                    return block_entry(
                        context,
                        args.entry_id,
                        kind=kind,
                        days=days,
                        expected_revision=args.expected_revision,
                    )
            if args.lexicon_command == "blacklist":
                if args.blacklist_command == "list":
                    return {"blacklist": list_blacklist(context, kind=args.kind)}
                if args.blacklist_command == "add":
                    kind, days = _blacklist_kind(args)
                    return add_blacklist(context, args.term, kind=kind, days=days)
                if args.blacklist_command == "update":
                    kind, days = _blacklist_kind(args)
                    return update_blacklist(
                        context,
                        args.blacklist_id,
                        kind=kind,
                        days=days,
                        expected_revision=args.expected_revision,
                    )
                if args.blacklist_command == "unblock":
                    return unblock_blacklist(
                        context,
                        args.blacklist_id,
                        expected_revision=args.expected_revision,
                    )
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


def _dispatch_pack(args: argparse.Namespace, store: OfficialPackStore) -> dict[str, Any]:
    if args.pack_command == "list":
        return store.list_packs(args.catalog)
    if args.pack_command == "setup":
        return store.setup(args.catalog, domains=args.domains)
    if args.pack_command == "install":
        return store.install(
            catalog=args.catalog, pack_ids=args.pack_ids, domains=args.domains
        )
    if args.pack_command == "uninstall":
        return store.uninstall(args.pack_ids)
    if args.pack_command == "update":
        return store.update(catalog=args.catalog)
    if args.pack_command == "status":
        return store.status()
    if args.pack_command == "repair":
        return store.repair(catalog=args.catalog)
    raise CueFlowError("unsupported Official Pack command")


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
    elif args.command == "lexicon":
        if isinstance(exc, LexiconRunFailedError):
            run_id = exc.run_id
        elif (
            args.lexicon_command == "suggestions"
            and args.suggestions_command == "retry"
        ):
            try:
                run_id = str(context.registry.lexicon_work_item(args.work_item_id)["run_id"])
            except CueFlowError:
                pass

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
    if invocation is not None and args.command not in {"reference", "lexicon"}:
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
    elif args.command == "lexicon" and run_id is not None:
        failed_items = [
            row
            for row in context.registry.lexicon_work_items_for_run(run_id)
            if row["status"] in {"failed", "interrupted"}
        ]
        payload["next_actions"] = [
            {
                "action": "lexicon suggestions retry",
                "work_item_id": str(row["work_item_id"]),
                "run_id": run_id,
                "requires_explicit_user_action": True,
            }
            for row in failed_items
        ] + [{"action": "lexicon suggestions status"}]
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
    payload: MappingLike = {
        "status": "failed",
        "error": str(exc) or type(exc).__name__,
        "run_id": None,
        "next_actions": [],
    }
    if isinstance(exc, SuppressionConflictError):
        payload["normalized_surface_form"] = exc.normalized_surface_form
        payload["conflicts"] = list(exc.conflicts)
        payload["choices"] = ["unblock_and_add", "cancel"]
    return payload


if __name__ == "__main__":
    raise SystemExit(main())
