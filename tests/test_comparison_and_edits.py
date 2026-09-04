from __future__ import annotations

import pytest

from cueflow.acoustic_adjudication import plan_disagreement_windows
from cueflow.asr_comparison import classify_hunk
from cueflow.asr_contracts import TimedUnit
from cueflow.edit_resolution import Edit, resolve_dual_edits
from cueflow.errors import ContractError


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


def test_window_merge_never_crosses_30_seconds() -> None:
    disputes = [
        {"disagreement_id": "a", "start": 0, "end": 1},
        {"disagreement_id": "b", "start": 1, "end": 2},
    ]
    windows = plan_disagreement_windows(
        disputes, "ab", [TimedUnit("a", 10_000, 12_000), TimedUnit("b", 31_000, 33_000)],
        60_000,
    )["windows"]
    assert len(windows) == 2
    assert all(window["global_end_ms"] - window["global_start_ms"] <= 30_000 for window in windows)


def test_exact_resolver_only_auto_patches_identical_interval_and_replacement() -> None:
    base = "We work with Grok. Another Grok appears."
    source = "We work with Grok."
    edit = Edit(source, "Grok", "Groq")
    payload = resolve_dual_edits(base, [edit], [edit])
    assert payload["corrected_preview"] == "We work with Groq. Another Grok appears."
    assert payload["review_items"] == []


def test_singleton_conflict_and_ambiguous_anchor_go_to_review() -> None:
    base = "Grok is here. Grok is there."
    qwen = [Edit("Grok is here.", "Grok", "Groq")]
    singleton = resolve_dual_edits(base, qwen, [])
    assert singleton["resolved_edits"] == []
    assert singleton["lexical_disagreements"]
    assert not singleton["review_items"]
    conflict = resolve_dual_edits(
        base,
        qwen,
        [Edit("Grok is here.", "Grok", "GROQ")],
    )
    assert conflict["lexical_disagreements"][0]["reason"] == "conflict"
    ambiguous = resolve_dual_edits(base, [Edit("Grok", "Grok", "Groq")], [])
    assert ambiguous["review_items"][0]["reason"] == "invalid_locator"


def test_empty_original_is_rejected() -> None:
    with pytest.raises(ContractError, match="original"):
        from cueflow.edit_resolution import parse_edits_json

        parse_edits_json(
            {"edits": [{"source_sentence": "Qwen38", "original": "", "replacement": "."}]}
        )
