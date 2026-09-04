from __future__ import annotations

import pytest

from cueflow.acoustic_adjudication import adjudicate, plan_disagreement_windows
from cueflow.asr_contracts import TimedUnit
from cueflow.edit_resolution import Edit, resolve_dual_edits


def _dispute(base: str, old: str, qwen: str, kimi: str) -> dict:
    return resolve_dual_edits(base, [Edit(base, old, qwen)], [Edit(base, old, kimi)])[
        "lexical_disagreements"
    ][0]


@pytest.mark.parametrize(
    ("glm", "status", "winner"),
    [
        ("We use Groq for inference.", "resolved", "Groq"),
        ("We use grok for inference.", "resolved", "Grok"),
        ("We use ACIE for inference.", "resolved", "ACIE"),
        ("We use Grock for inference.", "review", None),
        ("Grok occurs elsewhere.", "review", None),
        ("We use Groq for inference. We use Grok for inference.", "review", None),
        ("We use Groq for inference. We use Groq for inference.", "review", None),
    ],
)
def test_only_unique_exact_contextual_candidate_wins(glm: str, status: str, winner: str) -> None:
    base = "We use ACIE for inference."
    dispute = _dispute(base, "ACIE", "Groq", "Grok")
    result = adjudicate(base, dispute, glm)
    assert result["status"] == status
    if winner is not None:
        assert result["selected_text"] == winner


def test_casefold_collision_remains_ambiguous() -> None:
    base = "This is groke."
    dispute = _dispute(base, "groke", "Groq", "GROQ")
    assert adjudicate(base, dispute, "This is groq.")["reason"] == "AMBIGUOUS_CANDIDATE_MATCH"


def test_no_pinyin_or_nearest_candidate() -> None:
    base = "今天谈周红易的事情"
    dispute = _dispute(base, "周红易", "周鸿祎", "周宏祎")
    assert adjudicate(base, dispute, "今天谈周红衣的事情")["reason"] == "NO_EXACT_CANDIDATE_MATCH"


def test_long_or_unmapped_cases_are_local_plan_outcomes() -> None:
    disputes = [
        {"disagreement_id": "long", "start": 0, "end": 1},
        {"disagreement_id": "short", "start": 1, "end": 2},
    ]
    plan = plan_disagreement_windows(
        disputes,
        "ab",
        [TimedUnit("a", 0, 31_000), TimedUnit("b", 40_000, 41_000)],
        50_000,
    )
    assert plan["unavailable"] == [
        {"disagreement_id": "long", "reason": "CORE_WINDOW_OUT_OF_BOUNDS"}
    ]
    assert plan["windows"][0]["disagreement_ids"] == ["short"]
    assert plan_disagreement_windows(disputes, "ab", (), 50_000)["windows"] == []


def test_padding_can_shrink_and_union_cannot_cross_limit() -> None:
    disputes = [
        {"disagreement_id": "a", "start": 0, "end": 1},
        {"disagreement_id": "b", "start": 1, "end": 2},
    ]
    plan = plan_disagreement_windows(
        disputes,
        "ab",
        [TimedUnit("a", 1_000, 30_000), TimedUnit("b", 31_000, 33_000)],
        50_000,
    )
    assert len(plan["windows"]) == 2
    assert all(w["global_end_ms"] - w["global_start_ms"] <= 30_000 for w in plan["windows"])


def test_timed_index_does_not_restart_at_beginning() -> None:
    plan = plan_disagreement_windows(
        [{"disagreement_id": "a", "start": 0, "end": 1}],
        "ab",
        [TimedUnit("b", 0, 100), TimedUnit("a", 100, 200)],
        500,
    )
    assert plan["unavailable"][0]["reason"] == "TIME_MAPPING_UNAVAILABLE"


def test_partial_time_mapping_cannot_authorize_whole_dispute_window() -> None:
    plan = plan_disagreement_windows(
        [{"disagreement_id": "partial", "start": 0, "end": 3}],
        "abc",
        [TimedUnit("a", 0, 100), TimedUnit("c", 200, 300)],
        500,
    )
    assert not plan["windows"]
    assert plan["unavailable"][0]["reason"] == "TIME_MAPPING_UNAVAILABLE"
