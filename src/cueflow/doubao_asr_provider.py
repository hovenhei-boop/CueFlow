from __future__ import annotations

import os
import time
import uuid
from collections.abc import Mapping, Sequence
from typing import Any

import httpx

from cueflow.asr_contracts import AsrResult, ProviderMetadata, TimedUnit
from cueflow.config import DOUBAO_ASR_MODEL, CloudJobConfig
from cueflow.errors import (
    ContractError,
    DeliveryAmbiguousError,
    ProviderError,
    ProviderUnavailableError,
)

DOUBAO_SUBMIT_URL = "https://openspeech.bytedance.com/api/v3/auc/bigmodel/submit"
DOUBAO_QUERY_URL = "https://openspeech.bytedance.com/api/v3/auc/bigmodel/query"
DOUBAO_RESOURCE_ID = "volc.bigasr.auc"


def build_doubao_request(
    media_url: str, user_keywords: Sequence[str], *, uid: str
) -> dict[str, Any]:
    request: dict[str, Any] = {
        "model_name": DOUBAO_ASR_MODEL,
        "show_utterances": True,
        "enable_ddc": False,
    }
    if user_keywords:
        request["corpus"] = {
            "context": {"hotwords": [{"word": keyword} for keyword in user_keywords]}
        }
    return {"user": {"uid": uid}, "audio": {"url": media_url}, "request": request}


class DoubaoFileAsrProvider:
    provider = "volcengine-bigasr"
    model = DOUBAO_ASR_MODEL

    def __init__(
        self, client: httpx.Client | None = None, config: CloudJobConfig | None = None
    ) -> None:
        self._client = client
        self._owns_client = client is None
        self._config = config or CloudJobConfig()

    def transcribe(self, media_url: str, *, user_keywords: Sequence[str]) -> AsrResult:
        app_key = os.getenv("DOUBAO_APP_KEY")
        access_key = os.getenv("DOUBAO_ACCESS_KEY")
        api_key = os.getenv("DOUBAO_API_KEY")
        if not api_key and (not app_key or not access_key):
            raise ProviderUnavailableError(
                "Doubao ASR requires DOUBAO_API_KEY or DOUBAO_APP_KEY and DOUBAO_ACCESS_KEY"
            )
        uid = app_key or "cueflow"
        task_id = str(uuid.uuid4())
        headers = {
            "X-Api-Resource-Id": DOUBAO_RESOURCE_ID,
            "X-Api-Request-Id": task_id,
            "X-Api-Sequence": "-1",
            "Content-Type": "application/json",
        }
        if api_key:
            headers["X-Api-Key"] = api_key
        else:
            headers["X-Api-App-Key"] = str(app_key)
            headers["X-Api-Access-Key"] = str(access_key)
        client = self._client or httpx.Client(timeout=self._config.request_timeout_seconds)
        self._client = client
        started = time.monotonic()
        try:
            try:
                response = client.post(
                    DOUBAO_SUBMIT_URL,
                    headers=headers,
                    json=build_doubao_request(media_url, user_keywords, uid=uid),
                )
            except httpx.RequestError as exc:
                raise DeliveryAmbiguousError(
                    "Doubao ASR submit may have been delivered; automatic retry is forbidden"
                ) from exc
            _validate_doubao_response(response, "Doubao ASR submit")
            result = self._poll(client, headers)
            text, units = parse_doubao_result(result)
            metadata = ProviderMetadata(
                provider=self.provider,
                requested_model=self.model,
                resolved_model=self.model,
                response_id=response.headers.get("X-Tt-Logid") or task_id,
                elapsed_ms=round((time.monotonic() - started) * 1000),
            )
            return AsrResult(text, units, metadata)
        finally:
            if self._owns_client:
                client.close()
                self._client = None

    def _poll(self, client: httpx.Client, headers: Mapping[str, str]) -> Mapping[str, Any]:
        deadline = time.monotonic() + self._config.poll_timeout_seconds
        while True:
            response = client.post(DOUBAO_QUERY_URL, headers=headers, json={})
            status = response.headers.get("X-Api-Status-Code")
            if status == "20000000":
                return _json_object(response, "Doubao ASR query")
            if status not in {"20000001", "20000002", "20000003"}:
                _validate_doubao_response(response, "Doubao ASR query")
            if time.monotonic() >= deadline:
                raise ProviderUnavailableError("Doubao ASR query timed out")
            time.sleep(self._config.poll_interval_seconds)

    def close(self) -> None:
        if self._owns_client and self._client is not None:
            self._client.close()
            self._client = None


def parse_doubao_result(value: Mapping[str, Any]) -> tuple[str, tuple[TimedUnit, ...]]:
    result = _object(value.get("result"), "Doubao result")
    text = _nonempty(result.get("text"), "Doubao result.text")
    utterances = result.get("utterances")
    if not isinstance(utterances, list) or not utterances:
        raise ContractError("Doubao ASR requires show_utterances=true timestamps")
    units: list[TimedUnit] = []
    for utterance_raw in utterances:
        utterance = _object(utterance_raw, "Doubao utterance")
        units.append(_timed_unit(utterance))
    return text, tuple(units)


def _timed_unit(value: Mapping[str, Any]) -> TimedUnit:
    text = _nonempty(value.get("text"), "Doubao timed unit text")
    start = value.get("start_time")
    end = value.get("end_time")
    if not isinstance(start, int) or not isinstance(end, int) or start < 0 or end <= start:
        raise ContractError("Doubao ASR returned invalid timestamps")
    confidence = value.get("confidence")
    metadata = {"provider_value": confidence} if isinstance(confidence, (int, float)) else None
    return TimedUnit(text, start, end, metadata)


def _validate_doubao_response(response: httpx.Response, operation: str) -> None:
    if response.is_error:
        raise ProviderError(f"{operation} failed with HTTP {response.status_code}")
    status = response.headers.get("X-Api-Status-Code")
    if status is not None and status != "20000000":
        message = response.headers.get("X-Api-Message", "unknown error")
        raise ProviderError(f"{operation} failed: {status} {message}")


def _json_object(response: httpx.Response, operation: str) -> Mapping[str, Any]:
    try:
        return _object(response.json(), operation)
    except ValueError as exc:
        raise ContractError(f"{operation} returned invalid JSON") from exc


def _object(value: Any, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ContractError(f"{name} must be an object")
    return value


def _nonempty(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ContractError(f"{name} must be a non-empty string")
    return value
