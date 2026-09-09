from __future__ import annotations

from collections.abc import Sequence

import httpx
import pytest

from cueflow.base_asr_provider import QwenFiletransProvider
from cueflow.config import CloudJobConfig
from cueflow.errors import ContractError, DeliveryAmbiguousError, ProviderError


@pytest.fixture(autouse=True)
def credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DASHSCOPE_API_KEY", "fixture-api-secret")


def _client(
    responses: Sequence[httpx.Response | httpx.RequestError],
) -> tuple[httpx.Client, list[httpx.Request]]:
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        response = responses[len(requests) - 1]
        if isinstance(response, httpx.RequestError):
            raise response
        return response

    return httpx.Client(transport=httpx.MockTransport(handle)), requests


def _submitted() -> httpx.Response:
    return httpx.Response(200, json={"output": {"task_id": "qwen-task"}})


def test_query_network_failure_is_ambiguous_and_keeps_task_id() -> None:
    client, requests = _client([_submitted(), httpx.ReadTimeout("network interrupted")])
    with client, pytest.raises(DeliveryAmbiguousError) as caught:
        QwenFiletransProvider(client).transcribe(
            "https://media.example/audio.wav", user_keywords=[]
        )
    assert caught.value.metadata.response_id == "qwen-task"
    assert len(requests) == 2


def test_terminal_task_failure_keeps_task_id() -> None:
    client, requests = _client(
        [_submitted(), httpx.Response(200, json={"output": {"task_status": "FAILED"}})]
    )
    with client, pytest.raises(ProviderError) as caught:
        QwenFiletransProvider(client).transcribe(
            "https://media.example/audio.wav", user_keywords=[]
        )
    assert caught.value.metadata.response_id == "qwen-task"
    assert len(requests) == 2


def test_terminal_query_failure_keeps_task_id() -> None:
    client, _ = _client(
        [_submitted(), httpx.Response(200, json={"output": {"task_status": "FAILED"}})]
    )
    with client, pytest.raises(ProviderError) as caught:
        QwenFiletransProvider(client).transcribe(
            "https://media.example/audio.wav", user_keywords=[]
        )
    assert caught.value.metadata.response_id == "qwen-task"


def test_query_timeout_is_ambiguous_and_keeps_task_id() -> None:
    client, requests = _client(
        [_submitted(), httpx.Response(200, json={"output": {"task_status": "RUNNING"}})]
    )
    with client, pytest.raises(DeliveryAmbiguousError) as caught:
        QwenFiletransProvider(
            client, CloudJobConfig(poll_interval_seconds=0, poll_timeout_seconds=0)
        ).transcribe("https://media.example/audio.wav", user_keywords=[])
    assert caught.value.metadata.response_id == "qwen-task"
    assert len(requests) == 2


def test_completed_result_contract_failure_keeps_task_id() -> None:
    client, _ = _client(
        [
            _submitted(),
            httpx.Response(
                200,
                json={
                    "output": {
                        "task_status": "SUCCEEDED",
                        "results": [{"transcripts": []}],
                    }
                },
            ),
        ]
    )
    with client, pytest.raises(ContractError) as caught:
        QwenFiletransProvider(client).transcribe(
            "https://media.example/audio.wav", user_keywords=[]
        )
    assert caught.value.metadata.response_id == "qwen-task"
