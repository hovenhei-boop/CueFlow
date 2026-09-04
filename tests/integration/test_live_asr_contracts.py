from __future__ import annotations

import os

import pytest

from cueflow.base_asr_provider import QwenFiletransProvider
from cueflow.doubao_asr_provider import DoubaoFileAsrProvider

LIVE_URL = os.getenv("CUEFLOW_LIVE_ASR_URL")


@pytest.mark.skipif(
    not LIVE_URL or not os.getenv("DASHSCOPE_API_KEY"),
    reason="requires CUEFLOW_LIVE_ASR_URL and DASHSCOPE_API_KEY; this is a paid live probe",
)
def test_qwen_special_word_filter_object_is_accepted_by_live_filetrans() -> None:
    result = QwenFiletransProvider().transcribe(
        str(LIVE_URL), user_keywords=[".NET", "GPT-5.6"]
    )
    assert result.source_text
    assert result.timed_units

@pytest.mark.skipif(
    not LIVE_URL
    or not (
        os.getenv("DOUBAO_API_KEY")
        or (os.getenv("DOUBAO_APP_KEY") and os.getenv("DOUBAO_ACCESS_KEY"))
    ),
    reason="requires a live URL and Doubao credentials; this is a paid live probe",
)
def test_doubao_accepts_product_cap_of_100_inline_hotwords() -> None:
    keywords = [f"CueFlowLiveKeyword{index:03d}" for index in range(100)]
    result = DoubaoFileAsrProvider().transcribe(str(LIVE_URL), user_keywords=keywords)
    assert result.source_text
    assert result.timed_units
