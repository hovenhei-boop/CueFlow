from __future__ import annotations

from copy import deepcopy

import pytest

from cueflow.config import SelectionConfig
from cueflow.conflict_selection import (
    apply_selections,
    build_merge_plan,
    build_selection_batches,
    validate_decisions,
)
from cueflow.edit_resolution import apply_resolved_payload
from cueflow.errors import ContractError
from cueflow.schema import validate_payload
from cueflow.text_diff import build_text_map


@pytest.mark.parametrize(
    ("base", "qwen", "kimi", "expected", "count"),
    [
        ("甲乙丙", "甲乙丙", "甲乙丙", "甲乙丙", 0),
        ("甲乙丙", "甲X丙", "甲乙丙", "甲X丙", 0),
        ("甲乙丙", "甲X丙", "甲X丙", "甲X丙", 0),
        ("甲乙丙", "甲X丙", "甲Y丙", "甲乙丙", 1),
        ("甲乙丙", "甲X丙", "甲XY", "甲乙丙", 1),
        ("甲乙丙", "甲X丙", "甲X丙Z", "甲X丙Z", 0),
        ("甲乙丙", "甲丙", "甲乙丙", "甲丙", 0),
        ("甲乙丙", "前甲乙丙后", "甲乙丙", "前甲乙丙后", 0),
        ("甲乙丙", "甲X乙丙", "甲Y乙丙", "甲乙丙", 1),
        ("aaaa", "aaa", "aaaa", "aaa", 0),
        (
            "H264 NET nodejs C",
            "H.264 .NET node.js C++",
            "H264 NET nodejs C",
            "H.264 .NET node.js C++",
            0,
        ),
        ("甲，乙", "丙。丁", "丙！丁", "甲，乙", 1),
    ],
)
def test_complete_components(base: str, qwen: str, kimi: str, expected: str, count: int) -> None:
    plan = build_merge_plan(base, base, qwen, kimi)
    assert plan["corrected_preview"] == expected
    assert len(plan["cases"]) == count
    validate_payload("merge_plan", plan)


@pytest.mark.parametrize(
    ("base", "variant"),
    [
        ("", "前后"),
        ("原文", ""),
        ("aaaa", "aaa"),
        ("abcabc", "xabc"),
        ("é", "e\u0301"),
        ("A", "a"),
        ("𠀀前后", "前𠀀后"),
    ],
)
def test_raw_unicode_round_trip(base: str, variant: str) -> None:
    mapping = build_text_map(base, variant)
    patches = [
        dict(start=c.start, end=c.end, original=base[c.start : c.end], replacement=c.replacement)
        for c in mapping.changes
    ]
    assert apply_resolved_payload(base, patches) == variant


def test_peer_opaque_boundary_is_unavailable_not_an_invented_candidate() -> None:
    plan = build_merge_plan("aBCd", "aXYZd", "aB1d", "aB2d")
    assert "peer" not in plan["cases"][0]["candidates"]
    assert build_text_map("aBCd", "aXYZd").boundary(2) is None


def test_keep_empty_and_deduplicated_sources_preserve_exact_text() -> None:
    plan = build_merge_plan("甲乙", "甲X乙", "甲X乙", "甲Y乙")
    batches, reviews = build_selection_batches(plan)
    assert not reviews
    batch = batches[0]
    case = batch["request"]["cases"][0]
    assert len(case["candidates"]) == 3
    assert len(case["versions"]) == 4
    keep = dict(case_id=case["case_id"], candidate_id=case["keep_candidate_id"])
    patches = apply_selections("甲乙", batch, [keep])
    assert patches[0]["replacement"] == ""
    assert apply_resolved_payload("甲乙", patches) == "甲乙"


def test_no_third_text_or_casefold_and_unrelated_accepted_change_survives() -> None:
    plan = build_merge_plan("甲乙丙丁", "甲P丙丁", "甲X丙尾", "甲x丙丁")
    batches, _ = build_selection_batches(plan)
    batch = batches[0]
    case = batch["request"]["cases"][0]
    assert {o["text"] for o in case["candidates"]} == {"乙", "P", "X", "x"}
    decisions = [dict(case_id=case["case_id"], candidate_id=case["keep_candidate_id"])]
    patches = [*plan["resolved_edits"], *apply_selections("甲乙丙丁", batch, decisions)]
    assert apply_resolved_payload("甲乙丙丁", patches) == "甲乙丙尾"


@pytest.mark.parametrize(
    "bad",
    [
        {},
        {"decisions": []},
        {"decisions": [{"case_id": "wrong", "candidate_id": "c1"}]},
        {"decisions": [{"case_id": "case", "candidate_id": "third"}]},
        {"decisions": [{"case_id": "case", "candidate_id": "c1", "text": "new"}]},
        {"decisions": [{"case_id": "case", "candidate_id": "c1"}] * 2},
    ],
)
def test_strict_decision_coverage(bad: dict) -> None:
    with pytest.raises(ContractError):
        validate_decisions(
            bad, {"cases": [{"case_id": "case", "candidates": [{"candidate_id": "c1"}]}]}
        )


def test_context_maps_same_content_after_offset_shift_and_is_bounded() -> None:
    before, after = "前" * 600, "后" * 600
    base = before + "甲" + after
    plan = build_merge_plan(
        base, "额外" + before + "乙" + after, before + "丙" + after, before + "丁" + after
    )
    batches, _ = build_selection_batches(plan)
    assert batches == build_selection_batches(plan)[0]
    for version in batches[0]["request"]["cases"][0]["versions"]:
        assert version["before"] == "前" * 500
        assert version["after"] == "后" * 500
        assert len(version["target"]) == 1


def test_budget_routes_to_review_without_truncating_or_rejecting_large_edits() -> None:
    plan = build_merge_plan("甲", "甲", "乙" * 100, "丙" * 100)
    assert len(plan["cases"]) == 1
    batches, review = build_selection_batches(plan, SelectionConfig(max_input_bytes=100))
    assert not batches and len(review) == 1
    assert review[0]["candidates"]["qwen"] == "乙" * 100
    singleton = build_merge_plan("甲", "甲", "乙" * 100, "甲")
    assert singleton["corrected_preview"] == "乙" * 100


def test_forged_merge_provenance_is_rejected() -> None:
    plan = build_merge_plan("甲乙", "甲乙", "甲丙", "甲乙")
    forged = deepcopy(plan)
    forged["resolved_edits"][0]["support"]["source_intervals"]["qwen"] = [0, 2]
    with pytest.raises(ContractError, match="provenance"):
        validate_payload("merge_plan", forged)


def test_duplicate_insertions_at_one_coordinate_are_not_concatenated() -> None:
    with pytest.raises(ContractError, match="overlap"):
        apply_resolved_payload(
            "甲乙",
            [
                dict(start=1, end=1, original="", replacement="X"),
                dict(start=1, end=1, original="", replacement="Y"),
            ],
        )


def test_adjacent_disagreements_without_a_base_anchor_are_one_component() -> None:
    plan = build_merge_plan("ab", "ab", "xb", "ay")
    assert not plan["resolved_edits"]
    assert len(plan["cases"]) == 1
    case = plan["cases"][0]
    assert (case["start"], case["end"], case["original"]) == (0, 2, "ab")
    assert case["candidates"]["qwen"] == "xb"
    assert case["candidates"]["kimi"] == "ay"
    assert "xy" not in case["candidates"].values()


def test_nonempty_exact_base_anchor_allows_independent_components() -> None:
    plan = build_merge_plan("abc", "abc", "xbc", "aby")
    assert not plan["cases"]
    assert [(item["start"], item["end"]) for item in plan["resolved_edits"]] == [
        (0, 1),
        (2, 3),
    ]
    assert plan["corrected_preview"] == "xby"
