from __future__ import annotations

import pytest

from cueflow.asr_comparison import compare_asr
from cueflow.conflict_selection import build_merge_plan


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
    assert not any("requires_glm" in item for item in hunks)


def test_sealed_resolution_rejects_pending_selection_or_review() -> None:
    from cueflow.errors import ContractError
    from cueflow.schema import validate_payload

    payload = {
        "run_id": "run_test",
        "base_text": "原稿",
        "resolved_edits": [],
        "review_items": [],
        "pending_selection": 1,
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
        artifact_kind="merge_plan",
        scope_key="global",
        producer=Producer("test", "1", None, None, "sha256:" + "0" * 64),
        inputs=[],
        payload=build_merge_plan("原文", "原文", "原文", "原文"),
    )
    old = replace(envelope, schema_version="6.0.0")
    with pytest.raises(ContractError, match="unsupported schema_version"):
        old.validate()
