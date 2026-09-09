from __future__ import annotations

from typing import Any

import pytest
from test_correction_contract import _chunk, _client

from cueflow.cloud_stream import CompletedResponseError, strict_json
from cueflow.config import GLM_SELECTION_MODEL
from cueflow.errors import ContractError, DeliveryAmbiguousError
from cueflow.glm_selection_provider import GlmSelectionProvider, load_selection_prompt


def request() -> dict[str, Any]:
    return {
        "cases": [
            {
                "case_id": "case",
                "keep_candidate_id": "a",
                "candidates": [
                    {"candidate_id": "a", "text": "原文"},
                    {"candidate_id": "b", "text": "候选"},
                ],
                "versions": [],
            }
        ]
    }


def test_glm_uses_only_text_and_dedicated_prompt_with_auto_search(monkeypatch: Any) -> None:
    monkeypatch.setenv("ZHIPU_API_KEY", "fixture")
    chunk = _chunk('{"decisions":[{"case_id":"case","candidate_id":"b"}]}')
    chunk.web_search = [{"title": "fixture source", "link": "https://example.org/source"}]
    chunk.usage = {"total_tokens": 123}
    client, completions = _client([[chunk]])
    result = GlmSelectionProvider(lambda **_: client).select(request())
    assert result.decisions[0]["candidate_id"] == "b"
    assert result.metadata.usage == {"total_tokens": 123}
    assert result.metadata.search_results[0]["title"] == "fixture source"
    sent = completions.calls[0]
    assert sent["model"] == GLM_SELECTION_MODEL == "glm-5.2"
    assert sent["tool_choice"] == "auto"
    assert sent["tools"] == [
        {
            "type": "web_search",
            "web_search": {"enable": True, "search_engine": "search_pro", "search_result": True},
        }
    ]
    assert sent["extra_body"] == {"thinking": {"type": "disabled"}}
    assert sent["max_tokens"] == 1024
    prompt = load_selection_prompt()[0]
    assert sent["messages"] == [
        {"role": "system", "content": prompt},
        {
            "role": "user",
            "content": __import__("json").dumps(
                request(), ensure_ascii=False, separators=(",", ":")
            ),
        },
    ]
    assert "允许按需自动联网核验" in prompt
    assert "不得创造、拼接或输出新文字" in prompt


@pytest.mark.parametrize(
    "text",
    [
        '{"decisions":[]}',
        '{"decisions":[{"case_id":"case","candidate_id":"new"}]}',
        '```json\n{"decisions":[]}\n```',
        '{"decisions":[],"decisions":[]}',
    ],
)
def test_completed_bad_selection_exposes_metadata_without_hidden_retries(
    monkeypatch: Any, text: str
) -> None:
    monkeypatch.setenv("ZHIPU_API_KEY", "fixture")
    client, completions = _client([[_chunk(text)]])
    with pytest.raises(CompletedResponseError) as caught:
        GlmSelectionProvider(lambda **_: client).select(request())
    assert caught.value.metadata.response_id == "response"
    assert len(completions.calls) == 1


def test_selection_missing_completion_marker_is_ambiguous(monkeypatch: Any) -> None:
    monkeypatch.setenv("ZHIPU_API_KEY", "fixture")
    chunk = _chunk('{"decisions":[]}')
    chunk.choices[0].finish_reason = None
    client, completions = _client([[chunk]])
    with pytest.raises(DeliveryAmbiguousError):
        GlmSelectionProvider(lambda **_: client).select(request())
    assert len(completions.calls) == 1


@pytest.mark.parametrize("text", ['{"x":NaN}', '{"x":Infinity}', '{"x":1,"x":2}'])
def test_json_does_not_accept_duplicate_keys_or_nonfinite_numbers(text: str) -> None:
    with pytest.raises(ContractError):
        strict_json(text)
