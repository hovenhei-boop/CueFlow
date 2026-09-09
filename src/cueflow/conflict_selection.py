from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any

from cueflow.canonical import hash_json
from cueflow.config import SelectionConfig
from cueflow.edit_resolution import apply_resolved_payload
from cueflow.errors import ContractError
from cueflow.text_diff import TextChange, TextMap, build_text_map

MERGE_POLICY = "raw-codepoint-components-v2"


def _patch(base: str, start: int, end: int, text: str, reason: str) -> dict[str, Any]:
    return dict(start=start, end=end, original=base[start:end], replacement=text, resolution=reason)


def _is_stable_anchor(
    base: str,
    variants: Mapping[str, str],
    maps: Mapping[str, TextMap],
    start: int,
    end: int,
) -> bool:
    if end <= start:
        return False
    anchor = base[start:end]
    for source in ("qwen", "kimi"):
        interval = maps[source].interval(start, end)
        if interval is None:
            return False
        left, right = interval
        if variants[source][left:right] != anchor:
            return False
    return True


def build_merge_plan(base: str, peer: str, qwen: str, kimi: str) -> dict[str, Any]:
    variants = dict(base=base, peer=peer, qwen=qwen, kimi=kimi)
    maps = {source: build_text_map(base, text) for source, text in variants.items()}
    changes = sorted(
        ((source, change) for source in ("qwen", "kimi") for change in maps[source].changes),
        key=lambda entry: (entry[1].start, entry[1].end, entry[0]),
    )
    groups: list[list[tuple[str, TextChange]]] = []
    end = -1
    for source, change in changes:
        # Components may be separated only by a non-empty Base interval that
        # both correction transcripts preserve exactly. A SequenceMatcher
        # opcode alone is not sufficient evidence of a stable anchor.
        if groups and (
            change.start < end
            or not _is_stable_anchor(base, variants, maps, end, change.start)
        ):
            groups[-1].append((source, change))
        else:
            groups.append([(source, change)])
        end = max(end, change.end)
    accepted: list[dict[str, Any]] = []
    cases: list[dict[str, Any]] = []
    for group in groups:
        start, end = min(c.start for _, c in group), max(c.end for _, c in group)
        candidates: dict[str, str] = {"base": base[start:end]}
        intervals: dict[str, list[int]] = {"base": [start, end]}
        for source in ("qwen", "kimi", "peer"):
            # Peer insertions at an interval's right edge belong to the next
            # interval, except EOF. Q/K insertions already belong to this group.
            include_end = source != "peer" or end == len(base) or start == end
            interval = maps[source].interval(start, end, include_end_insert=include_end)
            if interval is not None:
                a, b = interval
                candidates[source] = variants[source][a:b]
                intervals[source] = [a, b]
        record: dict[str, Any] = dict(
            start=start,
            end=end,
            original=base[start:end],
            candidates=candidates,
            source_intervals=intervals,
        )
        if not {"qwen", "kimi"} <= candidates.keys():
            raise ContractError("Correction component cannot map to its source transcripts")
        q, k, original = candidates["qwen"], candidates["kimi"], candidates["base"]
        if q == k or q == original or k == original:
            text = k if q == original else q
            if text != original:
                accepted.append(
                    {
                        **_patch(base, start, end, text, "agreement" if q == k else "singleton"),
                        "support": record,
                    }
                )
        else:
            record["case_id"] = "case_" + hash_json(record).removeprefix("sha256:")[:24]
            cases.append(record)
    return dict(
        base_text=base,
        variants=variants,
        resolved_edits=accepted,
        cases=cases,
        review_items=[],
        merge_policy=MERGE_POLICY,
        corrected_preview=apply_resolved_payload(base, accepted),
    )


def _context_bounds(base: str, start: int, end: int, config: SelectionConfig) -> tuple[int, int]:
    left, right = max(0, start - config.context_chars), min(len(base), end + config.context_chars)
    stops = "。！？!?\n"
    while left > max(0, start - config.max_context_chars) and base[left - 1] not in stops:
        left -= 1
    while right < min(len(base), end + config.max_context_chars) and base[right - 1] not in stops:
        right += 1
    return left, right


def _selection_case(
    plan: Mapping[str, Any], case: Mapping[str, Any], config: SelectionConfig
) -> dict[str, Any]:
    base = str(plan["base_text"])
    left, right = _context_bounds(base, case["start"], case["end"], config)
    # Stable hash ordering hides vendor labels without losing replayability.
    texts = sorted(
        set(case["candidates"].values()),
        key=lambda text: hash_json([case["case_id"], text, "candidate-order-v1"]),
    )
    options = [{"candidate_id": f"c{index + 1}", "text": text} for index, text in enumerate(texts)]
    ids = {item["text"]: item["candidate_id"] for item in options}
    sources = sorted(
        case["source_intervals"], key=lambda source: hash_json([case["case_id"], source])
    )
    versions: list[dict[str, Any]] = []
    provenance: dict[str, Any] = {}
    for index, source in enumerate(sources):
        text = plan["variants"][source]
        mapping = build_text_map(base, text)
        a, b = case["source_intervals"][source]
        # If context edge crosses a replacement, expand that context to the
        # nearest exact mapped edge; never truncate or expand the selected core.
        context_left, context_right = left, right
        while context_left > 0 and mapping.boundary(context_left) is None:
            context_left -= 1
        while context_right < len(base) and mapping.boundary(context_right) is None:
            context_right += 1
        x, y = mapping.boundary(context_left), mapping.boundary(context_right, after_insert=True)
        if x is None or y is None:
            raise ContractError("Context could not be mapped")
        version_id = f"v{index + 1}"
        versions.append(
            dict(
                version_id=version_id,
                candidate_id=ids[case["candidates"][source]],
                before=text[x:a],
                target=text[a:b],
                after=text[b:y],
            )
        )
        provenance[version_id] = dict(source=source, interval=[a, b], context_interval=[x, y])
    request = dict(
        case_id=case["case_id"],
        candidates=options,
        versions=versions,
        keep_candidate_id=ids[case["original"]],
    )
    return dict(
        request=request,
        provenance=provenance,
        start=case["start"],
        end=case["end"],
        original=case["original"],
    )


def build_selection_batches(
    plan: Mapping[str, Any], config: SelectionConfig | None = None
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    chosen = config or SelectionConfig()
    batches: list[dict[str, Any]] = []
    reviews: list[dict[str, Any]] = []
    pending: list[dict[str, Any]] = []

    def size(items: list[dict[str, Any]]) -> int:
        return len(
            json.dumps(
                {"cases": [item["request"] for item in items]},
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
        )

    def finish() -> None:
        if pending:
            batch = dict(cases=list(pending), request={"cases": [c["request"] for c in pending]})
            batch["batch_id"] = "batch_" + hash_json(batch).removeprefix("sha256:")[:24]
            batches.append(batch)
            pending.clear()

    for case in plan["cases"]:
        item = _selection_case(plan, case, chosen)
        if size([item]) > chosen.max_input_bytes:
            reviews.append(
                {
                    **case,
                    "reason": "selection_input_budget_exceeded",
                    "review_id": "rev_" + case["case_id"],
                }
            )
            continue
        if len(pending) == chosen.max_cases or size([*pending, item]) > chosen.max_input_bytes:
            finish()
        pending.append(item)
    finish()
    return batches, reviews


def validate_decisions(value: Any, request: Mapping[str, Any]) -> list[dict[str, str]]:
    if not isinstance(value, dict) or set(value) != {"decisions"}:
        raise ContractError("Selection must contain only decisions")
    decisions = value["decisions"]
    if not isinstance(decisions, list):
        raise ContractError("decisions must be an array")
    allowed = {c["case_id"]: {v["candidate_id"] for v in c["candidates"]} for c in request["cases"]}
    seen: set[str] = set()
    for decision in decisions:
        if not isinstance(decision, dict) or set(decision) != {"case_id", "candidate_id"}:
            raise ContractError("Decision fields do not match the contract")
        case_id, candidate_id = decision["case_id"], decision["candidate_id"]
        if (
            not isinstance(case_id, str)
            or not isinstance(candidate_id, str)
            or case_id in seen
            or case_id not in allowed
            or candidate_id not in allowed[case_id]
        ):
            raise ContractError("Unknown or duplicate case/candidate ID")
        seen.add(case_id)
    if seen != allowed.keys():
        raise ContractError("Selection must cover every case exactly once")
    return [dict(item) for item in decisions]


def validate_batch(batch: Mapping[str, Any]) -> None:
    import hashlib

    cases = batch.get("cases")
    if not isinstance(cases, list) or not 1 <= len(cases) <= SelectionConfig().max_cases:
        raise ContractError("Invalid selection batch size")
    if batch.get("request") != {"cases": [case["request"] for case in cases]}:
        raise ContractError("Batch request does not match frozen cases")
    seen: set[str] = set()
    for case in cases:
        request = case["request"]
        identity = request["case_id"]
        if not isinstance(identity, str) or identity in seen:
            raise ContractError("Duplicate/invalid case ID")
        seen.add(identity)
        options = request["candidates"]
        if not 2 <= len(options) <= 4:
            raise ContractError("Selection requires two to four distinct candidates")
        by_id = {option["candidate_id"]: option["text"] for option in options}
        if (
            len(by_id) != len(options)
            or len(set(by_id.values())) != len(options)
            or not all(isinstance(text, str) for text in by_id.values())
        ):
            raise ContractError("Candidates must have distinct IDs and exact text")
        if by_id.get(request["keep_candidate_id"]) != case["original"]:
            raise ContractError("KEEP must be frozen Base text")
        if (
            not 0 <= case["start"] <= case["end"]
            or len(case["original"]) != case["end"] - case["start"]
        ):
            raise ContractError("Invalid selection core interval")
        for version in request["versions"]:
            if version["target"] != by_id.get(version["candidate_id"]):
                raise ContractError("Version target is not the referenced candidate")
            provenance = case["provenance"][version["version_id"]]
            a, b = provenance["interval"]
            x, y = provenance["context_interval"]
            if not 0 <= x <= a <= b <= y or (
                len(version["before"]),
                len(version["target"]),
                len(version["after"]),
            ) != (a - x, b - a, y - b):
                raise ContractError("Version context coordinates are inconsistent")
    core = {"cases": cases, "request": batch["request"]}
    if batch.get("batch_id") != "batch_" + hash_json(core).removeprefix("sha256:")[:24]:
        raise ContractError("Batch identity does not match its frozen input")
    prompt = batch.get("prompt")
    if (
        not isinstance(prompt, str)
        or batch.get("prompt_sha256")
        != "sha256:" + hashlib.sha256(prompt.encode("utf-8")).hexdigest()
    ):
        raise ContractError("Selection prompt hash mismatch")


def apply_selections(
    base: str, batch: Mapping[str, Any], decisions: Sequence[Mapping[str, str]]
) -> list[dict[str, Any]]:
    checked = validate_decisions({"decisions": list(decisions)}, batch["request"])
    cases = {c["request"]["case_id"]: c for c in batch["cases"]}
    patches: list[dict[str, Any]] = []
    for decision in checked:
        case = cases[decision["case_id"]]
        if base[case["start"] : case["end"]] != case["original"]:
            raise ContractError("Selection no longer matches frozen Base")
        option = next(
            c
            for c in case["request"]["candidates"]
            if c["candidate_id"] == decision["candidate_id"]
        )
        patches.append(
            {
                **_patch(base, case["start"], case["end"], option["text"], "selection"),
                "decision": dict(decision),
            }
        )
    return patches
