from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from cueflow.correction_provider import (
    CorrectionRequest,
    QwenCorrectionProvider,
    load_correction_prompt,
    parse_correction_response,
)
from cueflow.errors import ContractError, DeliveryAmbiguousError, ProviderError


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


def test_invalid_completed_json_is_exposed_for_independent_attempt_accounting(
    monkeypatch: Any,
) -> None:
    from cueflow.cloud_stream import CompletedResponseError

    monkeypatch.setenv("DASHSCOPE_API_KEY", "test")
    monkeypatch.setenv("DASHSCOPE_BASE_URL", "https://example.invalid/v1")
    client, completions = _client([[_chunk("not json")]])
    with pytest.raises(CompletedResponseError) as caught:
        QwenCorrectionProvider(client_factory=lambda **_: client).correct(_request())
    assert len(completions.calls) == 1
    assert caught.value.metadata.response_id == "response"
    assert caught.value.diagnostic()["raw_response"] == "not json"


def test_transport_ambiguity_is_never_automatically_retried(monkeypatch: Any) -> None:
    monkeypatch.setenv("DASHSCOPE_API_KEY", "test")
    monkeypatch.setenv("DASHSCOPE_BASE_URL", "https://example.invalid/v1")
    client, completions = _client([RuntimeError("transport interrupted")])
    provider = QwenCorrectionProvider(client_factory=lambda **_: client)

    with pytest.raises(DeliveryAmbiguousError) as caught:
        provider.correct(_request())

    assert len(completions.calls) == 1
    assert caught.value.metadata.provider == "dashscope-openai-compatible"
    assert caught.value.metadata.requested_model
    assert caught.value.metadata.response_id is None
    assert caught.value.metadata.usage is None
    assert caught.value.metadata.elapsed_ms is not None
    assert caught.value.metadata.elapsed_ms >= 0


def test_partial_stream_metadata_survives_transport_ambiguity(monkeypatch: Any) -> None:
    monkeypatch.setenv("DASHSCOPE_API_KEY", "test")
    monkeypatch.setenv("DASHSCOPE_BASE_URL", "https://example.invalid/v1")
    chunk = _chunk('{"corrected_text":"partial"}')
    chunk.choices[0].finish_reason = None
    chunk.usage = {"total_tokens": 17}
    chunk.web_search = [{"title": "partial evidence"}]

    def interrupted_stream() -> Any:
        yield chunk
        raise RuntimeError("transport interrupted after one chunk")

    client, completions = _client([interrupted_stream()])
    with pytest.raises(DeliveryAmbiguousError) as caught:
        QwenCorrectionProvider(client_factory=lambda **_: client).correct(_request())

    assert len(completions.calls) == 1
    assert caught.value.metadata.resolved_model == "qwen-max-current"
    assert caught.value.metadata.response_id == "response"
    assert caught.value.metadata.usage == {"total_tokens": 17}
    assert caught.value.metadata.search_results == ({"title": "partial evidence"},)
    assert caught.value.metadata.elapsed_ms is not None
    assert caught.value.metadata.elapsed_ms >= 0


def test_explicit_http_failure_stays_provider_error_with_metadata(monkeypatch: Any) -> None:
    class HttpFailure(RuntimeError):
        status_code = 503

    monkeypatch.setenv("DASHSCOPE_API_KEY", "test")
    monkeypatch.setenv("DASHSCOPE_BASE_URL", "https://example.invalid/v1")
    client, completions = _client([HttpFailure("service unavailable")])

    with pytest.raises(ProviderError) as caught:
        QwenCorrectionProvider(client_factory=lambda **_: client).correct(_request())

    assert not isinstance(caught.value, DeliveryAmbiguousError)
    assert str(caught.value) == "dashscope-openai-compatible explicit HTTP failure: 503"
    assert caught.value.metadata.provider == "dashscope-openai-compatible"
    assert caught.value.metadata.response_id is None
    assert caught.value.metadata.elapsed_ms is not None
    assert caught.value.metadata.elapsed_ms >= 0
    assert len(completions.calls) == 1


def test_prompt_keeps_full_recovery_rules_and_fulltext_contract() -> None:
    prompt, digest = load_correction_prompt()
    assert "# 1. 核心目标：恢复口播，而不是编辑内容" in prompt
    assert '{"corrected_text":"..."}' in prompt
    assert "修改字符数" in prompt
    assert '"edits"' not in prompt
    assert digest.startswith("sha256:") and len(digest) == 71


@pytest.mark.parametrize("finish", [None, "length"])
def test_incomplete_stream_never_becomes_a_successful_proposal(
    monkeypatch: Any,
    finish: str | None,
) -> None:
    monkeypatch.setenv("DASHSCOPE_API_KEY", "test")
    monkeypatch.setenv("DASHSCOPE_BASE_URL", "https://example.invalid/v1")
    chunk = _chunk('{"corrected_text":"This mentions Groq."}')
    chunk.choices[0].finish_reason = finish
    client, completions = _client([[chunk], [chunk]])
    provider = QwenCorrectionProvider(client_factory=lambda **_: client)
    error = DeliveryAmbiguousError if finish is None else ContractError
    with pytest.raises(error) as caught:
        provider.correct(_request())
    assert len(completions.calls) == 1
    assert caught.value.metadata.response_id == "response"
    assert caught.value.metadata.elapsed_ms is not None
    assert caught.value.metadata.elapsed_ms >= 0


def test_full_peer_search_model_and_disabled_sdk_retries(monkeypatch: Any) -> None:
    from cueflow.config import QWEN_CORRECTION_MODEL

    monkeypatch.setenv("DASHSCOPE_API_KEY", "test")
    monkeypatch.setenv("DASHSCOPE_BASE_URL", "https://example.invalid/v1")
    client, completions = _client([[_chunk('{"corrected_text":"This mentions Groq."}')]])
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


def test_nonempty_base_rejects_empty_completed_correction() -> None:
    with pytest.raises(ContractError, match="non-empty"):
        parse_correction_response('{"corrected_text":""}')


def test_provider_wraps_empty_completed_correction_with_paid_diagnostics(
    monkeypatch: Any,
) -> None:
    from cueflow.cloud_stream import CompletedResponseError

    monkeypatch.setenv("DASHSCOPE_API_KEY", "test")
    monkeypatch.setenv("DASHSCOPE_BASE_URL", "https://example.invalid/v1")
    raw = '{"corrected_text":""}'
    client, completions = _client([[_chunk(raw)]])
    with pytest.raises(CompletedResponseError) as caught:
        QwenCorrectionProvider(client_factory=lambda **_: client).correct(_request())
    assert len(completions.calls) == 1
    assert caught.value.metadata.response_id == "response"
    assert caught.value.diagnostic()["raw_response"] == raw
