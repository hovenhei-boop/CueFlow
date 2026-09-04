from __future__ import annotations

import os
import time
from collections.abc import Mapping, Sequence
from typing import Any

import httpx

from cueflow.asr_contracts import AsrResult, ProviderMetadata, TimedUnit
from cueflow.config import QWEN_ASR_MODEL, QWEN_HOTWORD_WEIGHT, CloudJobConfig
from cueflow.errors import (
    ContractError,
    DeliveryAmbiguousError,
    ProviderError,
    ProviderUnavailableError,
)

QWEN_SUBMIT_URL = "https://dashscope.aliyuncs.com/api/v1/services/audio/asr/transcription"
QWEN_TASK_URL = "https://dashscope.aliyuncs.com/api/v1/tasks/{task_id}"


def build_qwen_request(media_url: str, user_keywords: Sequence[str]) -> dict[str, Any]:
    parameters: dict[str, Any] = {
        "channel_id": [0],
        "special_word_filter": {"system_reserved_filter": False},
    }
    if user_keywords:
        parameters["vocabulary"] = [
            {"text": keyword, "weight": QWEN_HOTWORD_WEIGHT} for keyword in user_keywords
        ]
    return {
        "model": QWEN_ASR_MODEL,
        "input": {"file_urls": [media_url]},
        "parameters": parameters,
    }


class QwenFiletransProvider:
    provider = "dashscope-filetrans"
    model = QWEN_ASR_MODEL

    def __init__(
        self, client: httpx.Client | None = None, config: CloudJobConfig | None = None
    ) -> None:
        self._client = client
        self._owns_client = client is None
        self._config = config or CloudJobConfig()

    def transcribe(self, media_url: str, *, user_keywords: Sequence[str]) -> AsrResult:
        api_key = os.getenv("DASHSCOPE_API_KEY")
        if not api_key:
            raise ProviderUnavailableError("Qwen ASR requires DASHSCOPE_API_KEY")
        client = self._client or httpx.Client(timeout=self._config.request_timeout_seconds)
        self._client = client
        started = time.monotonic()
        try:
            try:
                response = client.post(
                    QWEN_SUBMIT_URL,
                    headers={
                        "Authorization": f"Bearer {api_key}",
                        "X-DashScope-Async": "enable",
                        "Content-Type": "application/json",
                    },
                    json=build_qwen_request(media_url, user_keywords),
                )
            except httpx.RequestError as exc:
                raise DeliveryAmbiguousError(
                    "Qwen ASR submit may have been delivered; automatic retry is forbidden"
                ) from exc
            _raise_http(response, "Qwen ASR submit")
            body = _json_object(response, "Qwen ASR submit")
            output = _object(body.get("output"), "Qwen ASR submit.output")
            task_id = _nonempty(output.get("task_id"), "Qwen ASR task_id")
            completed = self._poll(client, api_key, task_id)
            result_document = _qwen_result_document(client, completed)
            text, units = parse_qwen_result(result_document)
            metadata = ProviderMetadata(
                provider=self.provider,
                requested_model=self.model,
                resolved_model=_optional_text(completed.get("model")) or self.model,
                response_id=task_id,
                elapsed_ms=round((time.monotonic() - started) * 1000),
                usage=_mapping_or_none(completed.get("usage")),
            )
            return AsrResult(text, units, metadata)
        finally:
            if self._owns_client:
                client.close()
                self._client = None

    def _poll(self, client: httpx.Client, api_key: str, task_id: str) -> Mapping[str, Any]:
        deadline = time.monotonic() + self._config.poll_timeout_seconds
        while True:
            response = client.get(
                QWEN_TASK_URL.format(task_id=task_id),
                headers={"Authorization": f"Bearer {api_key}"},
            )
            _raise_http(response, "Qwen ASR query")
            body = _json_object(response, "Qwen ASR query")
            output = _object(body.get("output"), "Qwen ASR query.output")
            status = _nonempty(output.get("task_status"), "Qwen ASR task_status")
            if status == "SUCCEEDED":
                return body
            if status in {"FAILED", "CANCELED", "UNKNOWN"}:
                raise ProviderError(f"Qwen ASR task ended with status {status}")
            if time.monotonic() >= deadline:
                raise ProviderUnavailableError("Qwen ASR query timed out")
            time.sleep(self._config.poll_interval_seconds)

    def close(self) -> None:
        if self._owns_client and self._client is not None:
            self._client.close()
            self._client = None


def parse_qwen_result(value: Mapping[str, Any]) -> tuple[str, tuple[TimedUnit, ...]]:
    transcripts = value.get("transcripts")
    if not isinstance(transcripts, list) or not transcripts:
        raise ContractError("Qwen ASR result has no transcripts")
    transcript = _object(transcripts[0], "Qwen transcript")
    text = _nonempty(transcript.get("text"), "Qwen transcript.text")
    units: list[TimedUnit] = []
    sentences = transcript.get("sentences")
    if isinstance(sentences, list):
        for sentence_raw in sentences:
            sentence = _object(sentence_raw, "Qwen sentence")
            units.append(_qwen_timed_unit(sentence))
    if not units:
        raise ContractError("Qwen ASR result has no sentence or word timestamps")
    return text, tuple(units)


def _qwen_result_document(client: httpx.Client, completed: Mapping[str, Any]) -> Mapping[str, Any]:
    output = _object(completed.get("output"), "Qwen ASR output")
    results = output.get("results")
    if not isinstance(results, list) or not results:
        raise ContractError("Qwen ASR completed without results")
    result = _object(results[0], "Qwen ASR result")
    url = result.get("transcription_url")
    if isinstance(url, str) and url:
        response = client.get(url)
        _raise_http(response, "Qwen ASR transcript download")
        return _json_object(response, "Qwen ASR transcript download")
    return result


def _qwen_timed_unit(value: Mapping[str, Any]) -> TimedUnit:
    text = _nonempty(value.get("text"), "Qwen timed unit text")
    start = value.get("begin_time", value.get("start_time"))
    end = value.get("end_time")
    if not isinstance(start, int) or not isinstance(end, int) or start < 0 or end <= start:
        raise ContractError("Qwen ASR returned invalid timestamps")
    return TimedUnit(text, start, end)


def _raise_http(response: httpx.Response, operation: str) -> None:
    if response.is_error:
        raise ProviderError(f"{operation} failed with HTTP {response.status_code}")


def _json_object(response: httpx.Response, operation: str) -> Mapping[str, Any]:
    try:
        value = response.json()
    except ValueError as exc:
        raise ContractError(f"{operation} returned invalid JSON") from exc
    return _object(value, operation)


def _object(value: Any, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ContractError(f"{name} must be an object")
    return value


def _nonempty(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ContractError(f"{name} must be a non-empty string")
    return value


def _optional_text(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _mapping_or_none(value: Any) -> Mapping[str, Any] | None:
    return value if isinstance(value, Mapping) else None
