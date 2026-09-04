from __future__ import annotations

import pytest

from cueflow.errors import ContractError
from cueflow.job_inputs import normalize_keywords


def test_keywords_only_trim_and_exact_dedupe() -> None:
    assert normalize_keywords([" .NET ", "C++", "GPT-5.6", ".NET", "Ｓ＆Ｐ"]) == [
        ".NET",
        "C++",
        "GPT-5.6",
        "Ｓ＆Ｐ",
    ]


def test_empty_and_more_than_100_keywords_are_rejected() -> None:
    with pytest.raises(ContractError, match="empty"):
        normalize_keywords(["  "])
    with pytest.raises(ContractError, match="100"):
        normalize_keywords([f"k{index}" for index in range(101)])


def test_keyword_limit_applies_after_exact_deduplication() -> None:
    assert normalize_keywords(["C++"] * 101) == ["C++"]
