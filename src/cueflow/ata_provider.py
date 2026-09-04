from __future__ import annotations

import os
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import httpx

from cueflow.asr_contracts import ProviderMetadata, TimedUnit
from cueflow.config import ATA_PROVIDER, CloudJobConfig
from cueflow.errors import (
    ContractError,
    DeliveryAmbiguousError,
    ProviderError,
    ProviderUnavailableError,
)

ATA_SUBMIT_URL = "https://openspeech.bytedance.com/api/v1/vc/ata/submit"
ATA_QUERY_URL = "https://openspeech.bytedance.com/api/v1/vc/ata/query"
ATA_RESOURCE_ID = "volc.ata.default"


@dataclass(frozen=True)
class AlignmentResult:
    tokens: tuple[TimedUnit, ...]
    utterances: tuple[TimedUnit, ...]
    metadata: ProviderMetadata


def build_ata_submit_request(
    appid: str, media_url: str, transcript_text: str
) -> tuple[dict[str, str], dict[str, str]]:
    query = {"appid": appid, "caption_type": "speech", "sta_punc_mode": "3"}
    payload = {"url": media_url, "audio_text": transcript_text}
    return query, payload


class VolcengineAtaProvider:
    provider = ATA_PROVIDER
    model = "automatic-transcript-alignment"

    def __init__(
        self, client: httpx.Client | None = None, config: CloudJobConfig | None = None
    ) -> None:
        self._client = client
        self._owns_client = client is None
        self._config = config or CloudJobConfig()

    def align(self, media_url: str, transcript_text: str) -> AlignmentResult:
        appid = os.getenv("VOLCENGINE_ATA_APPID")
        token = os.getenv("VOLCENGINE_ATA_ACCESS_TOKEN")
        if not appid or not token:
            raise ProviderUnavailableError(
                "ATA requires VOLCENGINE_ATA_APPID and VOLCENGINE_ATA_ACCESS_TOKEN"
            )
        query, payload = build_ata_submit_request(appid, media_url, transcript_text)
        headers = {"Authorization": f"Bearer; {token}", "Resource-Id": ATA_RESOURCE_ID}
        client = self._client or httpx.Client(timeout=self._config.request_timeout_seconds)
        self._client = client
        started = time.monotonic()
        try:
            try:
                response = client.post(ATA_SUBMIT_URL, params=query, headers=headers, json=payload)
            except httpx.RequestError as exc:
                raise DeliveryAmbiguousError(
                    "ATA submit may have been delivered; automatic retry is forbidden"
                ) from exc
            body = _checked_json(response, "ATA submit")
            task_id = body.get("id", body.get("task_id"))
            if not isinstance(task_id, str) or not task_id:
                raise ContractError("ATA submit returned no task id")
            result = self._poll(client, appid, token, task_id)
            tokens, utterances = parse_ata_result(result)
            metadata = ProviderMetadata(
                provider=self.provider,
                requested_model=self.model,
                resolved_model=self.model,
                response_id=task_id,
                elapsed_ms=round((time.monotonic() - started) * 1000),
            )
            return AlignmentResult(tokens, utterances, metadata)
        finally:
            if self._owns_client:
                client.close()
                self._client = None

    def _poll(
        self, client: httpx.Client, appid: str, token: str, task_id: str
    ) -> Mapping[str, Any]:
        deadline = time.monotonic() + self._config.poll_timeout_seconds
        headers = {"Authorization": f"Bearer; {token}", "Resource-Id": ATA_RESOURCE_ID}
        params = {"appid": appid, "task_id": task_id}
        while True:
            response = client.get(ATA_QUERY_URL, params=params, headers=headers)
            body = _checked_json(response, "ATA query")
            status = body.get("status", body.get("code"))
            if status in {"success", "succeeded", "done", 1000, 0}:
                return body
            if status in {"failed", "error", "canceled"} or (
                isinstance(status, int) and status not in {1001, 1002}
            ):
                raise ProviderError(f"ATA task ended with status {status}")
            if time.monotonic() >= deadline:
                raise ProviderUnavailableError("ATA query timed out")
            time.sleep(self._config.poll_interval_seconds)

    def close(self) -> None:
        if self._owns_client and self._client is not None:
            self._client.close()
            self._client = None


def parse_ata_result(
    value: Mapping[str, Any],
) -> tuple[tuple[TimedUnit, ...], tuple[TimedUnit, ...]]:
    container: Mapping[str, Any] = value
    for key in ("result", "data", "response"):
        candidate = container.get(key)
        if isinstance(candidate, Mapping):
            container = candidate
    raw_utterances = container.get("utterances")
    if not isinstance(raw_utterances, list) or not raw_utterances:
        raise ContractError("ATA returned no utterances")
    utterances: list[TimedUnit] = []
    tokens: list[TimedUnit] = []
    for raw in raw_utterances:
        item = _object(raw, "ATA utterance")
        utterances.append(_timed(item, "ATA utterance"))
        words = item.get("words")
        if not isinstance(words, list):
            raise ContractError("ATA utterance returned no word timestamps")
        tokens.extend(_timed(_object(word, "ATA word"), "ATA word") for word in words)
    if not tokens:
        raise ContractError("ATA returned no word timestamps")
    return tuple(tokens), tuple(utterances)


def _timed(value: Mapping[str, Any], name: str) -> TimedUnit:
    text = value.get("text")
    start = value.get("start_time", value.get("start_ms"))
    end = value.get("end_time", value.get("end_ms"))
    if not isinstance(text, str) or not text:
        raise ContractError(f"{name} has invalid text")
    if not isinstance(start, int) or not isinstance(end, int) or start < 0 or end <= start:
        raise ContractError(f"{name} has invalid timestamps")
    return TimedUnit(text, start, end)


def _checked_json(response: httpx.Response, operation: str) -> Mapping[str, Any]:
    if response.is_error:
        raise ProviderError(f"{operation} failed with HTTP {response.status_code}")
    try:
        return _object(response.json(), operation)
    except ValueError as exc:
        raise ContractError(f"{operation} returned invalid JSON") from exc


def _object(value: Any, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ContractError(f"{name} must be an object")
    return value
