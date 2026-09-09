from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from cueflow.ata_provider import VolcengineAtaProvider
from cueflow.ata_result import parse_ata_result
from cueflow.config import CloudJobConfig
from cueflow.errors import ContractError, DeliveryAmbiguousError, ProviderError


@pytest.mark.parametrize("terminal", [0, "0", 1000, 1001, 1002, 4000, False, "done", None])
def test_query_uses_documented_codes_and_keeps_task_identity(
    monkeypatch: Any, terminal: Any
) -> None:
    monkeypatch.setenv("VOLCENGINE_ATA_APPID", "fixture")
    monkeypatch.setenv("VOLCENGINE_ATA_ACCESS_TOKEN", "secret")
    requests: list[httpx.Request] = []
    text = "  APS-C；H.264！\r\n.NET？ "
    raw = json.dumps({"code": terminal, "utterances": [
        {"text": text, "start_time": 0, "end_time": 100}
    ]}, ensure_ascii=False, indent=2).encode("utf-8")

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "POST":
            assert dict(request.url.params) == {
                "appid": "fixture", "caption_type": "speech", "sta_punc_mode": "3"
            }
            assert json.loads(request.content) == {
                "url": "https://example.org/media", "audio_text": text
            }
            return httpx.Response(200, json={"code": 0, "id": "job"})
        assert dict(request.url.params) == {"appid": "fixture", "id": "job", "blocking": "0"}
        if len(requests) == 2 or terminal is None:
            return httpx.Response(200, json={"code": 2000})
        return httpx.Response(200, content=raw)

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        provider = VolcengineAtaProvider(client, CloudJobConfig(
            poll_interval_seconds=0, poll_timeout_seconds=0 if terminal is None else 10
        ))
        if type(terminal) in (int, str) and terminal in (0, "0"):
            result = provider.align("https://example.org/media", text)
            assert result.raw_response == raw
            assert result.audio_text == text
            assert result.metadata.response_id == "job"
            assert parse_ata_result(raw) == [{"text": text, "start_ms": 0, "end_ms": 100}]
        else:
            with pytest.raises(
                DeliveryAmbiguousError if terminal is None else ProviderError
            ) as caught:
                provider.align("https://example.org/media", text)
            assert caught.value.metadata.response_id == "job"
    assert sum(request.method == "POST" for request in requests) == 1


@pytest.mark.parametrize("failure", ["transport", "json", "http", "object"])
def test_query_failure_after_submit_keeps_task_id(monkeypatch: Any, failure: str) -> None:
    monkeypatch.setenv("VOLCENGINE_ATA_APPID", "fixture")
    monkeypatch.setenv("VOLCENGINE_ATA_ACCESS_TOKEN", "secret")

    def respond(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(200, json={"code": 0, "id": "task-diagnostic"})
        if failure == "transport":
            raise httpx.ReadTimeout("fixture", request=request)
        if failure == "json":
            return httpx.Response(200, content=b"not-json")
        if failure == "object":
            return httpx.Response(200, json=[])
        return httpx.Response(503)

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        with pytest.raises((ContractError, ProviderError)) as caught:
            VolcengineAtaProvider(client).align("https://example.org/media", "全文")
    assert caught.value.metadata.response_id == "task-diagnostic"


def test_submit_error_is_not_polled(monkeypatch: Any) -> None:
    monkeypatch.setenv("VOLCENGINE_ATA_APPID", "fixture")
    monkeypatch.setenv("VOLCENGINE_ATA_ACCESS_TOKEN", "secret")

    def respond(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        return httpx.Response(200, json={"code": 1002, "id": "not-a-task"})

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        with pytest.raises(ProviderError, match="submit failed"):
            VolcengineAtaProvider(client).align("https://example.org/media", "全文")


@pytest.mark.parametrize("raw", [b"not-json", b"{}", b"[]", b'{"utterances":null}'])
def test_unreadable_result_shape_is_an_explicit_failure(raw: bytes) -> None:
    with pytest.raises(ContractError):
        parse_ata_result(raw)


@pytest.mark.parametrize("item", [
    None, {}, {"text": None, "start_time": 0, "end_time": 1},
    {"text": "a", "start_time": False, "end_time": 1},
    {"text": "a", "start_time": 0, "end_time": 1.5},
    {"text": "a", "start_time": "0", "end_time": 1},
    {"text": "a", "start_ms": 0, "end_ms": 1},
])
def test_parser_requires_only_current_sentence_field_types(item: Any) -> None:
    with pytest.raises(ContractError):
        parse_ata_result(json.dumps({"utterances": [item]}).encode())


@pytest.mark.parametrize("start,end", [(-10, 0), (15, 12), (0, 0), (99, 99)])
@pytest.mark.parametrize("words", [None, "unused", [{"text": ".", "start_time": False}]])
def test_parser_does_not_judge_timing_or_read_words(start: int, end: int, words: Any) -> None:
    result = parse_ata_result(json.dumps({"utterances": [{
        "text": "", "start_time": start, "end_time": end, "words": words
    }]}).encode())
    assert result == [{"text": "", "start_ms": start, "end_ms": end}]


def test_success_raw_survives_unreadable_sentence_structure(monkeypatch: Any) -> None:
    monkeypatch.setenv("VOLCENGINE_ATA_APPID", "fixture")
    monkeypatch.setenv("VOLCENGINE_ATA_ACCESS_TOKEN", "secret")
    raw = b'{ "code": 0, "utterances": [{"text": "missing timing"}] }'

    def respond(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(200, json={"code": 0, "id": "job"})
        return httpx.Response(200, content=raw)

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        result = VolcengineAtaProvider(client).align("https://example.org/media", "全文")
    assert result.raw_response == raw
    assert result.metadata.response_id == "job"
    with pytest.raises(ContractError):
        parse_ata_result(result.raw_response)
