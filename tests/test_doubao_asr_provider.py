from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any
from urllib.parse import quote

import httpx
import pytest

from cueflow.config import CloudJobConfig
from cueflow.doubao_asr_provider import (
    DOUBAO_QUERY_URL,
    DOUBAO_SUBMIT_URL,
    DoubaoFileAsrProvider,
    _validate_doubao_response,
)
from cueflow.errors import DeliveryAmbiguousError, ProviderError


@pytest.fixture(autouse=True)
def credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DOUBAO_API_KEY", "fixture-api-secret")
    monkeypatch.delenv("DOUBAO_APP_KEY", raising=False)
    monkeypatch.delenv("DOUBAO_ACCESS_KEY", raising=False)


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
    return httpx.Response(
        200, headers={"X-Api-Status-Code": "20000000", "X-Tt-Logid": "submit-log"}
    )


def _detail(error: ProviderError) -> dict[str, Any]:
    value = json.loads(str(error).split(": ", 1)[1])
    assert isinstance(value, dict)
    return value


@pytest.mark.parametrize("stage", ["submit", "query"])
@pytest.mark.parametrize("http_status", [302, 403, 503])
def test_non_2xx_preserves_business_diagnostics(stage: str, http_status: int) -> None:
    headers = {
        "X-Api-Status-Code": "45000030",
        "X-Api-Message": "resource access denied",
        "X-Tt-Logid": "provider-trace-123",
    }
    response = httpx.Response(
        http_status, headers=headers, json={"error": "resource access denied"}
    )
    responses = [response] if stage == "submit" else [_submitted(), response]
    client, requests = _client(responses)
    with client, pytest.raises(ProviderError) as caught:
        DoubaoFileAsrProvider(client).transcribe(
            "https://media.example/audio.mp3", user_keywords=[]
        )
    assert type(caught.value) is ProviderError
    detail = _detail(caught.value)
    assert detail == {
        "http_status": http_status,
        **headers,
        "body": response.text,
        "body_truncated": False,
    }
    assert len(requests) == (1 if stage == "submit" else 2)
    assert str(requests[-1].url) == (DOUBAO_SUBMIT_URL if stage == "submit" else DOUBAO_QUERY_URL)
    assert requests[-1].headers["X-Api-Key"] == "fixture-api-secret"
    assert requests[-1].headers["X-Api-Resource-Id"] == "volc.seedasr.auc"
    assert "X-Api-App-Key" not in requests[-1].headers
    assert "X-Api-Access-Key" not in requests[-1].headers
    if stage == "query":
        assert caught.value.metadata.response_id == requests[0].headers["X-Api-Request-Id"]


@pytest.mark.parametrize("business_status", ["20000000", "20000001", "20000002", "20000003"])
def test_query_non_2xx_cannot_be_hidden_by_success_or_pending_header(business_status: str) -> None:
    client, requests = _client(
        [
            _submitted(),
            httpx.Response(403, headers={"X-Api-Status-Code": business_status}),
        ]
    )
    with client, pytest.raises(ProviderError) as caught:
        DoubaoFileAsrProvider(client).transcribe(
            "https://media.example/audio.mp3", user_keywords=[]
        )
    assert _detail(caught.value)["X-Api-Status-Code"] == business_status
    assert len(requests) == 2


@pytest.mark.parametrize("body", [b"", b"upstream unavailable", b"\xff\xfeinvalid text"])
def test_missing_headers_and_non_json_body_do_not_mask_http_failure(body: bytes) -> None:
    with pytest.raises(ProviderError) as caught:
        _validate_doubao_response(httpx.Response(502, content=body), "Doubao ASR submit")
    assert _detail(caught.value) == {
        "http_status": 502,
        "X-Api-Status-Code": None,
        "X-Api-Message": None,
        "X-Tt-Logid": None,
        "body": body.decode("utf-8", errors="replace"),
        "body_truncated": False,
    }


def test_error_redaction_preserves_diagnostic_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    tos_key = "tos-access-private"
    tos_secret = 'tos-secret/+"\\value'
    authorization = "request-only-auth-secret"
    monkeypatch.setenv("TOS_ACCESS_KEY", tos_key)
    monkeypatch.setenv("TOS_SECRET_KEY", tos_secret)
    request = httpx.Request(
        "POST",
        DOUBAO_SUBMIT_URL,
        headers={"Authorization": "Bearer " + authorization, "X-Api-Key": "request-only-api-key"},
    )
    diagnostic = {
        "X-Api-Status-Code": "45000030",
        "X-Api-Message": "resource access denied",
        "X-Tt-Logid": "provider-trace-123",
    }
    body = json.dumps(
        {
            **diagnostic,
            "echo": [
                tos_key,
                tos_secret,
                quote(tos_secret, safe=""),
                "fixture-api-secret",
                authorization,
            ],
            "X-Api-Key": "unconfigured-echoed-api-key",
            "Authorization": "Bearer unconfigured-echoed-authorization",
            "TOS_SECRET_KEY": "unconfigured-echoed-tos-secret",
            "url": "https://media.example/a?X-Tos-Credential=unknown-credential%2Fdate"
            "&X-Tos-Signature=unknown-signature&X-Tos-Security-Token=unknown-session-token",
        }
    )
    response = httpx.Response(403, request=request, headers=diagnostic, text=body)
    with pytest.raises(ProviderError) as caught:
        _validate_doubao_response(response, "Doubao ASR submit")
    detail = _detail(caught.value)
    for name, value in diagnostic.items():
        assert detail[name] == value
        assert name in detail["body"]
        assert value in detail["body"]
    for secret in (
        tos_key,
        tos_secret,
        quote(tos_secret, safe=""),
        json.dumps(tos_secret)[1:-1],
        "fixture-api-secret",
        authorization,
        "request-only-api-key",
        "unconfigured-echoed-api-key",
        "unconfigured-echoed-authorization",
        "unconfigured-echoed-tos-secret",
        "unknown-credential",
        "unknown-signature",
        "unknown-session-token",
    ):
        assert secret not in detail["body"]
    assert "[REDACTED]" in detail["body"]
    assert response.text == body


def test_header_values_are_redacted_without_removing_diagnostic_names() -> None:
    response = httpx.Response(
        403,
        headers={
            "X-Api-Status-Code": "45000030",
            "X-Api-Message": "invalid key fixture-api-secret; Authorization: Bearer echoed-secret",
            "X-Tt-Logid": "trace-123",
        },
    )
    with pytest.raises(ProviderError) as caught:
        _validate_doubao_response(response, "Doubao ASR query")
    detail = _detail(caught.value)
    assert detail["X-Api-Status-Code"] == "45000030"
    assert detail["X-Tt-Logid"] == "trace-123"
    assert detail["X-Api-Message"] == "invalid key [REDACTED]; Authorization: [REDACTED]"


def test_body_is_redacted_before_truncation() -> None:
    body = "a" * 4090 + "fixture-api-secret" + "x" * 10000
    with pytest.raises(ProviderError) as caught:
        _validate_doubao_response(httpx.Response(403, text=body), "Doubao ASR submit")
    detail = _detail(caught.value)
    assert detail["body_truncated"] is True
    assert len(detail["body"]) == 4096
    assert detail["body"] == ("a" * 4090 + "[REDACTED]")[:4096]


def test_success_and_pending_polling_are_unchanged() -> None:
    text = "Authorization and X-Api-Key are header names."
    client, requests = _client(
        [
            _submitted(),
            httpx.Response(200, headers={"X-Api-Status-Code": "20000001"}),
            httpx.Response(
                200,
                headers={"X-Api-Status-Code": "20000000"},
                json={
                    "result": {
                        "text": text,
                        "utterances": [{"text": text, "start_time": 0, "end_time": 1000}],
                    },
                },
            ),
        ]
    )
    with client:
        result = DoubaoFileAsrProvider(client, CloudJobConfig(poll_interval_seconds=0)).transcribe(
            "https://media.example/audio.mp3",
            user_keywords=["C++"],
        )
    assert result.source_text == text
    assert result.timed_units[0].text == text
    assert result.metadata.response_id == requests[0].headers["X-Api-Request-Id"]
    assert len(requests) == 3
    assert [str(request.url) for request in requests] == [
        DOUBAO_SUBMIT_URL,
        DOUBAO_QUERY_URL,
        DOUBAO_QUERY_URL,
    ]
    assert all(request.headers["X-Api-Resource-Id"] == "volc.seedasr.auc" for request in requests)
    assert json.loads(requests[0].content)["request"]["model_name"] == "bigmodel"


def test_2xx_business_error_keeps_existing_exception_text() -> None:
    response = httpx.Response(
        200,
        headers={
            "X-Api-Status-Code": "45000030",
            "X-Api-Message": "API_KEY is a field name",
        },
    )
    with pytest.raises(ProviderError) as caught:
        _validate_doubao_response(response, "Doubao ASR submit")
    assert str(caught.value) == "Doubao ASR submit failed: 45000030 API_KEY is a field name"


@pytest.mark.parametrize("stage", ["submit", "query"])
def test_network_exception_semantics_and_no_automatic_retry_are_unchanged(stage: str) -> None:
    timeout = httpx.ReadTimeout("network interrupted")
    responses = [timeout] if stage == "submit" else [_submitted(), timeout]
    client, requests = _client(responses)
    with client, pytest.raises(DeliveryAmbiguousError) as caught:
        DoubaoFileAsrProvider(client).transcribe(
            "https://media.example/audio.mp3", user_keywords=[]
        )
    assert len(requests) == (1 if stage == "submit" else 2)
    assert getattr(caught.value.metadata, "response_id", None) == (
        None if stage == "submit" else requests[0].headers["X-Api-Request-Id"]
    )


def test_query_timeout_is_ambiguous_and_keeps_submitted_request_id() -> None:
    client, requests = _client(
        [_submitted(), httpx.Response(200, headers={"X-Api-Status-Code": "20000001"})]
    )
    with client, pytest.raises(DeliveryAmbiguousError) as caught:
        DoubaoFileAsrProvider(
            client, CloudJobConfig(poll_interval_seconds=0, poll_timeout_seconds=0)
        ).transcribe("https://media.example/audio.mp3", user_keywords=[])
    assert caught.value.metadata.response_id == requests[0].headers["X-Api-Request-Id"]
    assert len(requests) == 2
