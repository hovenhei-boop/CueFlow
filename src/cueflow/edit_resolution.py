from __future__ import annotations

import difflib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from cueflow.asr_comparison import SENTENCE_PUNCTUATION, classify_hunk
from cueflow.canonical import hash_json
from cueflow.errors import ContractError


@dataclass(frozen=True)
class Edit:
    source_sentence: str
    original: str
    replacement: str

    def as_dict(self) -> dict[str, str]:
        return {
            "source_sentence": self.source_sentence,
            "original": self.original,
            "replacement": self.replacement,
        }


@dataclass(frozen=True)
class LocatedEdit:
    start: int
    end: int
    replacement: str
    edit: Edit

    def key(self) -> tuple[int, int, str]:
        return self.start, self.end, self.replacement


def parse_edits_json(value: Any) -> tuple[Edit, ...]:
    if not isinstance(value, Mapping) or set(value) != {"edits"}:
        raise ContractError("Correction response must contain only edits")
    raw_edits = value["edits"]
    if not isinstance(raw_edits, list):
        raise ContractError("Correction edits must be an array")
    edits: list[Edit] = []
    for raw in raw_edits:
        if not isinstance(raw, Mapping) or set(raw) != {
            "source_sentence",
            "original",
            "replacement",
        }:
            raise ContractError("Correction edit fields do not match the contract")
        source = raw["source_sentence"]
        original = raw["original"]
        replacement = raw["replacement"]
        if not isinstance(source, str) or not source:
            raise ContractError("source_sentence must be a non-empty string")
        if not isinstance(original, str) or not original:
            raise ContractError("original must be a non-empty exact source fragment")
        if not isinstance(replacement, str):
            raise ContractError("replacement must be a string")
        edits.append(Edit(source, original, replacement))
    return tuple(edits)


PROJECTION_POLICY = "separable-runs-v1"
MATCH_POLICY = "ascii-case-insensitive-v1"


def occurrences(text: str, fragment: str) -> list[int]:
    if not fragment:
        raise ContractError("empty locator")
    result: list[int] = []
    cursor = 0
    while (position := text.find(fragment, cursor)) >= 0:
        result.append(position)
        cursor = position + 1
    return result


def locate_edit(base: str, edit: Edit) -> LocatedEdit:
    sources = occurrences(base, edit.source_sentence)
    originals = occurrences(edit.source_sentence, edit.original)
    if len(sources) != 1 or len(originals) != 1:
        raise ContractError("source_sentence and original must each have one exact occurrence")
    start = sources[0] + originals[0]
    return LocatedEdit(start, start + len(edit.original), edit.replacement, edit)


def _runs(text: str, start: int, end: int) -> list[tuple[int, int, str]]:
    """Keep identifier punctuation lexical; never classify a mixed opcode wholesale."""
    runs: list[tuple[int, int, str]] = []
    cursor = start
    while cursor < end:
        stop = cursor + 1
        plain = text[cursor] in SENTENCE_PUNCTUATION or text[cursor].isspace()
        while stop < end and (text[stop] in SENTENCE_PUNCTUATION or text[stop].isspace()) == plain:
            stop += 1
        category = classify_hunk(text, text, cursor, stop, cursor, stop)
        if runs and runs[-1][2] == category:
            runs[-1] = (runs[-1][0], stop, category)
        else:
            runs.append((cursor, stop, category))
        cursor = stop
    return runs


def refine_change(
    base: str, candidate: str, a0: int, a1: int, b0: int, b1: int
) -> list[tuple[int, int, int, int]] | None:
    """Pair ordered homogeneous runs only; unmatched interior structure is unresolved."""
    left, right = _runs(base, a0, a1), _runs(candidate, b0, b1)
    if not left:
        return [(a0, a0, start, end) for start, end, _ in right]
    if not right:
        return [(start, end, b0, b0) for start, end, _ in left]
    prosody = "prosodic_format_only"
    # Only boundary formatting can be paired with an empty boundary. Interior
    # formatting has no safe position in a differently structured lexical edit.
    if left[0][2] != right[0][2]:
        if left[0][2] == prosody:
            right.insert(0, (b0, b0, prosody))
        else:
            left.insert(0, (a0, a0, prosody))
    if left[-1][2] != right[-1][2]:
        if left[-1][2] == prosody:
            right.append((b1, b1, prosody))
        else:
            left.append((a1, a1, prosody))
    if [x[2] for x in left] != [x[2] for x in right]:
        return None
    return [(a[0], a[1], b[0], b[1]) for a, b in zip(left, right, strict=True)]


def project_lexical_changes(base: str, edit: LocatedEdit) -> dict[str, Any]:
    candidate = base[: edit.start] + edit.replacement + base[edit.end :]
    original = base[edit.start : edit.end]
    matcher = difflib.SequenceMatcher(a=original, b=edit.replacement, autojunk=False)
    output: list[str] = []
    changes: list[dict[str, Any]] = []
    for tag, a0, a1, b0, b1 in matcher.get_opcodes():
        if tag == "equal":
            output.append(original[a0:a1])
            continue
        pieces = refine_change(
            base,
            candidate,
            edit.start + a0,
            edit.start + a1,
            edit.start + b0,
            edit.start + b1,
        )
        if pieces is None:
            return {
                "status": "unresolved",
                "reason": "UNSEPARABLE_MIXED_CHANGE",
                "policy": PROJECTION_POLICY,
                "changes": changes,
            }
        for x0, x1, y0, y1 in pieces:
            category = classify_hunk(base, candidate, x0, x1, y0, y1)
            before, after = base[x0:x1], candidate[y0:y1]
            output.append(before if category == "prosodic_format_only" else after)
            if before != after:
                changes.append(
                    {
                        "start": x0,
                        "end": x1,
                        "candidate_start": y0,
                        "candidate_end": y1,
                        "original": before,
                        "replacement": after,
                        "category": category,
                    }
                )
    return {
        "status": "resolved",
        "text": "".join(output),
        "policy": PROJECTION_POLICY,
        "changes": changes,
    }


def _patch(base: str, start: int, end: int, text: str, reason: str) -> dict[str, Any]:
    return {
        "start": start,
        "end": end,
        "original": base[start:end],
        "replacement": text,
        "resolution": reason,
    }


def _candidate(base: str, start: int, end: int, edits: Sequence[LocatedEdit]) -> str:
    value = base[start:end]
    for edit in sorted(edits, key=lambda item: item.start, reverse=True):
        value = value[: edit.start - start] + edit.replacement + value[edit.end - start :]
    return value


def resolve_dual_edits(
    base_text: str, qwen_edits: Sequence[Edit], kimi_edits: Sequence[Edit]
) -> dict[str, Any]:
    located: list[tuple[str, LocatedEdit]] = []
    reviews: list[dict[str, Any]] = []
    for arm, edits in (("qwen", qwen_edits), ("kimi", kimi_edits)):
        for edit in dict.fromkeys(edits):
            try:
                found = locate_edit(base_text, edit)
            except ContractError:
                reviews.append({"reason": "invalid_locator", "arm": arm, "edit": edit.as_dict()})
                continue
            if found.replacement != base_text[found.start : found.end]:
                located.append((arm, found))
    # Connected components organize disputes, not a repair of the former
    # accepted-survivor overlap check.
    groups: list[list[tuple[str, LocatedEdit]]] = []
    group_end = -1
    for item in sorted(located, key=lambda x: (x[1].start, x[1].end, x[0])):
        if groups and item[1].start < group_end:
            groups[-1].append(item)
        else:
            groups.append([item])
        group_end = max(group_end, item[1].end)
    accepted: list[dict[str, Any]] = []
    disputes: list[dict[str, Any]] = []
    ignored: list[dict[str, Any]] = []
    for group in groups:
        start = min(item.start for _, item in group)
        end = max(item.end for _, item in group)
        arms = {arm: [edit for key, edit in group if key == arm] for arm in ("qwen", "kimi")}
        record: dict[str, Any] = {
            "start": start,
            "end": end,
            "original": base_text[start:end],
            "qwen": [item.edit.as_dict() for item in arms["qwen"]],
            "kimi": [item.edit.as_dict() for item in arms["kimi"]],
        }
        if any(
            a.start < b.end and b.start < a.end
            for edits in arms.values()
            for index, a in enumerate(edits)
            for b in edits[index + 1 :]
        ):
            reviews.append({**record, "reason": "contradictory_arm_overlap"})
            continue
        candidates = {
            "base": base_text[start:end],
            **{arm: _candidate(base_text, start, end, edits) for arm, edits in arms.items()},
        }
        record["candidates"] = candidates
        same_span = all(
            len(edits) == 1 and edits[0].start == start and edits[0].end == end
            for edits in arms.values()
        )
        if same_span and candidates["qwen"] == candidates["kimi"]:
            accepted.append(
                {
                    **_patch(base_text, start, end, candidates["qwen"], "agreement"),
                    "support": record,
                }
            )
            continue
        projections: dict[str, Any] = {}
        for arm, located_edits in arms.items():
            parts = [project_lexical_changes(base_text, entry) for entry in located_edits]
            if any(part["status"] != "resolved" for part in parts):
                projections[arm] = {"status": "unresolved", "parts": parts}
            else:
                projected = base_text[start:end]
                for entry, part in sorted(
                    zip(located_edits, parts, strict=True), key=lambda x: x[0].start, reverse=True
                ):
                    projected = (
                        projected[: entry.start - start]
                        + str(part["text"])
                        + projected[entry.end - start :]
                    )
                projections[arm] = {"status": "resolved", "text": projected, "parts": parts}
        record["projections"] = projections
        ready = all(part["status"] == "resolved" for part in projections.values())
        if ready and all(part["text"] == candidates["base"] for part in projections.values()):
            ignored.append({**record, "resolution": "ignored_prosody"})
        elif same_span and ready and projections["qwen"]["text"] == projections["kimi"]["text"]:
            accepted.append(
                {
                    **_patch(
                        base_text,
                        start,
                        end,
                        projections["qwen"]["text"],
                        "lexical_agreement_ignore_prosody",
                    ),
                    "support": record,
                }
            )
        else:
            # Different spans are never mined for partial lexical agreement.
            reason = (
                "projection_unresolved"
                if not ready
                else "singleton"
                if not all(arms.values())
                else "conflict"
                if same_span
                else "different_spans"
            )
            record["reason"] = reason
            record["disagreement_id"] = "dis_" + hash_json(record).removeprefix("sha256:")
            disputes.append(record)
    for review in reviews:
        review["review_id"] = "rev_" + hash_json(review).removeprefix("sha256:")
    return {
        "base_text": base_text,
        "resolved_edits": accepted,
        "lexical_disagreements": disputes,
        "ignored_disagreements": ignored,
        "review_items": reviews,
        "projection_policy": PROJECTION_POLICY,
        "corrected_preview": apply_resolved_payload(base_text, accepted),
    }


def apply_located_edits(base_text: str, edits: Sequence[LocatedEdit]) -> str:
    return apply_resolved_payload(
        base_text,
        [_patch(base_text, item.start, item.end, item.replacement, "manual") for item in edits],
    )


def apply_resolved_payload(base_text: str, edits: Sequence[Mapping[str, Any]]) -> str:
    result = base_text
    previous_start = len(base_text) + 1
    for raw in sorted(edits, key=lambda item: int(item["start"]), reverse=True):
        start, end = int(raw["start"]), int(raw["end"])
        if not 0 <= start < end <= len(base_text) or end > previous_start:
            raise ContractError("resolved edits overlap or have an invalid Base interval")
        if base_text[start:end] != raw["original"]:
            raise ContractError("resolved edit no longer matches frozen Base")
        result = result[:start] + str(raw["replacement"]) + result[end:]
        previous_start = start
    return result
