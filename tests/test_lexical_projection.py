from __future__ import annotations

from typing import Any

import pytest

from cueflow.asr_comparison import compare_asr
from cueflow.edit_resolution import (
    Edit,
    locate_edit,
    project_lexical_changes,
    refine_change,
    resolve_dual_edits,
)


@pytest.mark.parametrize(
    ("base", "qwen", "kimi", "expected", "disputed"),
    [
        ("英为达，", "英伟达，", "英伟达。", "英伟达，", False),
        ("英为达", "英伟达，", "英伟达。", "英伟达", False),
        ("英为达，", "英伟达。", "英为达，", "英为达，", True),
        ("H264", "H.264", "H264", "H264", True),
        ("Black well", "Blackwell,", "Blackwell.", "Blackwell", False),
        ("今天很好，", "今天很好。", "今天很好！", "今天很好，", False),
        ("甲，", "乙。", "乙！", "乙，", False),
        ("甲，乙", "丙。丁", "丙！丁", "丙，丁", False),
        ("C", "C++", "C", "C", True),
        ("C", "C#", "C", "C", True),
        ("GPT56", "GPT-5.6", "GPT56", "GPT56", True),
        ("Groq", "Gr,oq", "Groq", "Groq", True),
        ("nodejs", "node.js", "nodejs", "nodejs", True),
        ("NET", ".NET", "NET", "NET", True),
    ],
)
def test_agreement_projection_examples(
    base: str,
    qwen: str,
    kimi: str,
    expected: str,
    disputed: bool,
) -> None:
    result = resolve_dual_edits(
        base,
        [Edit(base, base, qwen)],
        [Edit(base, base, kimi)],
    )
    assert result["corrected_preview"] == expected
    assert bool(result["lexical_disagreements"]) == disputed
    assert not result["review_items"]


def test_coarse_opcode_is_refined_not_classified_wholesale(monkeypatch: Any) -> None:
    import cueflow.edit_resolution as resolution

    class CoarseMatcher:
        def __init__(self, *, a: str, b: str, **_: Any) -> None:
            self.a, self.b = a, b

        def get_opcodes(self) -> list[tuple[str, int, int, int, int]]:
            return [("replace", 0, len(self.a), 0, len(self.b))]

    monkeypatch.setattr(resolution.difflib, "SequenceMatcher", CoarseMatcher)
    base = "英为达，"
    projection = project_lexical_changes(base, locate_edit(base, Edit(base, base, "英伟达。")))
    assert projection["text"] == "英伟达，"
    assert [item["category"] for item in projection["changes"]] == [
        "lexical",
        "prosodic_format_only",
    ]


def test_unseparable_mixed_block_fails_closed(monkeypatch: Any) -> None:
    import cueflow.edit_resolution as resolution

    assert refine_change("甲，乙", "丙丁", 0, 3, 0, 2) is None
    monkeypatch.setattr(resolution, "refine_change", lambda *args: None)
    result = resolve_dual_edits("甲，乙", [Edit("甲，乙", "甲，乙", "丙丁")], [])
    assert not result["resolved_edits"]
    assert result["lexical_disagreements"][0]["reason"] == "projection_unresolved"


def test_different_spans_do_not_mine_shared_lexical_content() -> None:
    base = "今天讲英为达的新显卡"
    result = resolve_dual_edits(
        base,
        [Edit(base, "英为达", "英伟达")],
        [Edit(base, "英为达的新", "英伟达最新")],
    )
    assert not result["resolved_edits"]
    dispute = result["lexical_disagreements"][0]
    assert dispute["reason"] == "different_spans"
    assert dispute["candidates"] == {
        "base": "英为达的新",
        "qwen": "英伟达的新",
        "kimi": "英伟达最新",
    }


def test_anchor_context_can_differ_but_base_span_must_match() -> None:
    base = "今天讲英为达的新显卡"
    result = resolve_dual_edits(
        base,
        [Edit(base, "英为达", "英伟达")],
        [Edit("讲英为达的新", "英为达", "英伟达。")],
    )
    assert result["corrected_preview"] == "今天讲英伟达的新显卡"


def test_overlapping_occurrences_are_not_unique() -> None:
    result = resolve_dual_edits("aaaa", [Edit("aaa", "aaa", "b")], [])
    assert result["review_items"][0]["reason"] == "invalid_locator"
    assert not result["lexical_disagreements"]


@pytest.mark.parametrize(
    ("left", "right", "category"),
    [
        ("今天很好，", "今天很好。", "prosodic_format_only"),
        ("英伟达", "英伟达，", "prosodic_format_only"),
        ("H264", "H.264", "lexical"),
        ("Blackwell", "Black well", "lexical"),
        ("Groq", "Gr,oq", "lexical"),
    ],
)
def test_asr_and_correction_share_classification(left: str, right: str, category: str) -> None:
    hunks = compare_asr(left, right, (), ())
    assert {item["category"] for item in hunks} == {category}
    projection = project_lexical_changes(left, locate_edit(left, Edit(left, left, right)))
    assert {item["category"] for item in projection["changes"]} == {category}
    assert not any("requires_glm" in item for item in hunks)


def test_persisted_projection_is_recomputed_and_cannot_be_forged() -> None:
    from copy import deepcopy

    from cueflow.errors import ContractError
    from cueflow.schema import validate_payload

    result = resolve_dual_edits(
        "英为达，",
        [Edit("英为达，", "英为达，", "英伟达，")],
        [Edit("英为达，", "英为达，", "英伟达。")],
    )
    validate_payload("agreement_resolution", result)
    forged = deepcopy(result)
    forged["resolved_edits"][0]["replacement"] = "别的公司，"
    with pytest.raises(ContractError, match="invent lexical"):
        validate_payload("agreement_resolution", forged)
    forged = deepcopy(result)
    forged["resolved_edits"][0]["support"]["projections"]["kimi"]["parts"] = []
    with pytest.raises(ContractError, match="provenance"):
        validate_payload("agreement_resolution", forged)


def test_sealed_resolution_rejects_pending_acoustic_or_review() -> None:
    from cueflow.errors import ContractError
    from cueflow.schema import validate_payload

    payload = {
        "run_id": "run_test",
        "base_text": "原稿",
        "resolved_edits": [],
        "review_items": [],
        "pending_acoustic": 1,
        "sealed": True,
        "corrected_preview": "原稿",
    }
    with pytest.raises(ContractError, match="pending work"):
        validate_payload("edit_resolution", payload)


def test_previous_schema_is_not_reinterpreted() -> None:
    from dataclasses import replace

    from cueflow.errors import ContractError
    from cueflow.schema import ArtifactEnvelope, Producer

    envelope = ArtifactEnvelope.create(
        artifact_kind="agreement_resolution",
        scope_key="global",
        producer=Producer("test", "1", None, None, "sha256:" + "0" * 64),
        inputs=[],
        payload=resolve_dual_edits("原文", [], []),
    )
    old = replace(envelope, schema_version="6.0.0")
    with pytest.raises(ContractError, match="unsupported schema_version"):
        old.validate()
