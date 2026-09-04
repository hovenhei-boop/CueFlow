from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

from cueflow.asr_comparison import TimedTextIndex
from cueflow.asr_contracts import TimedUnit
from cueflow.canonical import hash_json
from cueflow.config import EvidenceWindowConfig
from cueflow.edit_resolution import MATCH_POLICY
from cueflow.errors import ContractError


def plan_disagreement_windows(
    disagreements: Sequence[Mapping[str, Any]],
    base_text: str,
    units: Sequence[TimedUnit],
    duration_ms: int,
    config: EvidenceWindowConfig | None = None,
) -> dict[str, Any]:
    chosen = config or EvidenceWindowConfig()
    windows: list[dict[str, Any]] = []
    unavailable: list[dict[str, str]] = []
    try:
        index = TimedTextIndex(base_text, units)
    except ContractError:
        index = None
    for dispute in sorted(disagreements, key=lambda item: int(item["start"])):
        identity = str(dispute["disagreement_id"])
        interval = index.interval(int(dispute["start"]), int(dispute["end"])) if index else None
        if interval is None:
            unavailable.append({"disagreement_id": identity, "reason": "TIME_MAPPING_UNAVAILABLE"})
            continue
        start, end = interval
        if not 0 <= start < end <= duration_ms or end - start > chosen.max_duration_ms:
            unavailable.append({"disagreement_id": identity, "reason": "CORE_WINDOW_OUT_OF_BOUNDS"})
            continue
        extra = min(chosen.padding_ms, (chosen.max_duration_ms - (end - start)) // 2)
        start, end = max(0, start - extra), min(duration_ms, end + extra)
        if windows and start - windows[-1]["global_end_ms"] <= chosen.merge_gap_ms:
            prior = windows[-1]
            union_end = max(end, prior["global_end_ms"])
            if union_end - prior["global_start_ms"] <= chosen.max_duration_ms:
                prior["global_end_ms"] = union_end
                prior["disagreement_ids"].append(identity)
                continue
        windows.append(
            {"global_start_ms": start, "global_end_ms": end, "disagreement_ids": [identity]}
        )
    for window in windows:
        window["window_id"] = "win_" + hash_json(window).removeprefix("sha256:")
    return {
        "windows": windows,
        "unavailable": unavailable,
        "disagreement_ids": [str(item["disagreement_id"]) for item in disagreements],
    }


def _ascii_fold(text: str) -> str:
    return text.translate(str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz"))


def adjudicate(
    base_text: str,
    dispute: Mapping[str, Any],
    glm_text: str,
) -> dict[str, Any]:
    """Unique exact contextual match, never distance/pinyin/semantic ranking."""
    start, end = int(dispute["start"]), int(dispute["end"])
    left, right = base_text[max(0, start - 32) : start], base_text[end : end + 32]
    candidates = dict(dispute["candidates"])
    distinct = list(dict.fromkeys(str(value) for value in candidates.values()))
    matched: list[str] = []
    ambiguous_position = False
    for candidate in distinct:
        phrase = left + candidate + right
        pattern = ("^" if not left else "") + re.escape(_ascii_fold(phrase))
        if not right:
            pattern += "$"
        matches = list(re.finditer("(?=(" + pattern + "))", _ascii_fold(glm_text)))
        if len(matches) == 1:
            matched.append(candidate)
        elif matches:
            ambiguous_position = True
    result: dict[str, Any] = {
        "disagreement_id": dispute["disagreement_id"],
        "match_policy": MATCH_POLICY,
        "status": "review",
        "reason": "NO_EXACT_CANDIDATE_MATCH",
    }
    if ambiguous_position or len(matched) > 1:
        result["reason"] = "AMBIGUOUS_CANDIDATE_MATCH"
    elif len(matched) == 1:
        winner = matched[0]
        result.update(
            status="resolved",
            reason="UNIQUE_EXACT_CANDIDATE_MATCH",
            selected_text=winner,
            selected_arms=[arm for arm, text in candidates.items() if text == winner],
            action="keep" if winner == base_text[start:end] else "replace",
        )
    return result
