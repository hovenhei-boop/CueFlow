from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from cueflow.correction_provider import (
    CorrectionRequest,
    QwenCorrectionProvider,
    load_correction_prompt,
)
from cueflow.errors import ContractError, DeliveryAmbiguousError


def _request() -> CorrectionRequest:
    return CorrectionRequest(
        base_text="This mentions Grok.",
        peer_text="This mentions Groq.",
        references=(),
        user_keywords=("Groq",),
        comparison_hunks=(),
    )


def _chunk(text: str) -> Any:
    return SimpleNamespace(
        id="response",
        model="qwen-max-current",
        usage=None,
        choices=[SimpleNamespace(delta=SimpleNamespace(content=text), finish_reason="stop")],
    )


class _Completions:
    def __init__(self, responses: list[object]) -> None:
        self.responses = responses
        self.calls: list[dict[str, object]] = []

    def create(self, **kwargs: object) -> object:
        self.calls.append(kwargs)
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


def _client(responses: list[object]) -> tuple[Any, _Completions]:
    completions = _Completions(responses)
    client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    return client, completions


def test_invalid_strict_json_gets_one_identical_semantic_retry(monkeypatch: Any) -> None:
    monkeypatch.setenv("DASHSCOPE_API_KEY", "test")
    monkeypatch.setenv("DASHSCOPE_BASE_URL", "https://example.invalid/v1")
    client, completions = _client(
        [
            [_chunk("not json")],
            [
                _chunk(
                    '{"edits":[{"source_sentence":"This mentions Grok.",'
                    '"original":"Grok","replacement":"Groq"}]}'
                )
            ],
        ]
    )
    provider = QwenCorrectionProvider(client_factory=lambda **_: client)

    result = provider.correct(_request())

    assert result.edits[0].replacement == "Groq"
    assert len(completions.calls) == 2
    assert completions.calls[0] == completions.calls[1]


def test_transport_ambiguity_is_never_automatically_retried(monkeypatch: Any) -> None:
    monkeypatch.setenv("DASHSCOPE_API_KEY", "test")
    monkeypatch.setenv("DASHSCOPE_BASE_URL", "https://example.invalid/v1")
    client, completions = _client([RuntimeError("transport interrupted")])
    provider = QwenCorrectionProvider(client_factory=lambda **_: client)

    with pytest.raises(DeliveryAmbiguousError):
        provider.correct(_request())

    assert len(completions.calls) == 1


def test_prompt_keeps_full_recovery_rules_and_edits_contract() -> None:
    prompt, digest = load_correction_prompt()
    assert "# 1. 核心目标：恢复口播，而不是修改内容" in prompt
    assert "# 23. 输出契约" in prompt
    assert "正确形式没有在任何输入中出现，也允许恢复" in prompt
    assert '{"edits":[]}' in prompt
    assert "corrected_text；" in prompt
    assert digest.startswith("sha256:") and len(digest) == 71


@pytest.mark.parametrize("finish", [None, "length"])
def test_incomplete_stream_never_becomes_a_successful_proposal(
    monkeypatch: Any,
    finish: str | None,
) -> None:
    monkeypatch.setenv("DASHSCOPE_API_KEY", "test")
    monkeypatch.setenv("DASHSCOPE_BASE_URL", "https://example.invalid/v1")
    chunk = _chunk('{"edits":[]}')
    chunk.choices[0].finish_reason = finish
    client, completions = _client([[chunk], [chunk]])
    provider = QwenCorrectionProvider(client_factory=lambda **_: client)
    error = DeliveryAmbiguousError if finish is None else ContractError
    with pytest.raises(error):
        provider.correct(_request())
    assert len(completions.calls) == (1 if finish is None else 2)


def test_full_peer_search_model_and_disabled_sdk_retries(monkeypatch: Any) -> None:
    from cueflow.config import QWEN_CORRECTION_MODEL

    monkeypatch.setenv("DASHSCOPE_API_KEY", "test")
    monkeypatch.setenv("DASHSCOPE_BASE_URL", "https://example.invalid/v1")
    client, completions = _client([[_chunk('{"edits":[]}')]])
    options: dict[str, Any] = {}

    def factory(**kwargs: Any) -> Any:
        options.update(kwargs)
        return client

    QwenCorrectionProvider(client_factory=factory).correct(_request())
    assert options["max_retries"] == 0
    sent = completions.calls[0]
    assert sent["model"] == QWEN_CORRECTION_MODEL
    assert "Independent PeerTranscript" in str(sent["messages"])
    assert sent["extra_body"] == {
        "enable_search": True,
        "search_options": {"forced_search": True, "search_strategy": "max"},
    }
