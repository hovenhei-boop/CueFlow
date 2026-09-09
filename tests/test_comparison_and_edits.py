from __future__ import annotations

import pytest

from cueflow.asr_comparison import classify_hunk
from cueflow.conflict_selection import build_merge_plan


@pytest.mark.parametrize(
    ("base", "peer", "a0", "a1", "b0", "b1", "expected"),
    [
        ("完成了,接下来", "完成了，接下来", 3, 4, 3, 4, "prosodic_format_only"),
        ("Qwen38", "Qwen3.8", 5, 5, 5, 6, "lexical"),
        ("v17", "v1.7", 2, 2, 2, 3, "lexical"),
        ("A/B", "AB", 1, 2, 1, 1, "lexical"),
        ("foo-bar", "foobar", 3, 4, 3, 3, "lexical"),
    ],
)
def test_prosodic_format_classifier_is_conservative(
    base: str, peer: str, a0: int, a1: int, b0: int, b1: int, expected: str
) -> None:
    assert classify_hunk(base, peer, a0, a1, b0, b1) == expected


def test_merge_plan_only_auto_patches_identical_interval_and_replacement() -> None:
    base = "We work with Grok. Another Grok appears."
    source = "We work with Grok."
    corrected = base.replace(source, source.replace("Grok", "Groq"))
    payload = build_merge_plan(base, base, corrected, corrected)
    assert payload["corrected_preview"] == "We work with Groq. Another Grok appears."
    assert payload["review_items"] == []
