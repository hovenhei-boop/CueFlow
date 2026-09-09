from __future__ import annotations

import os
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import httpx

from cueflow.asr_contracts import ProviderMetadata
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
class AtaResponse:
    raw_response: bytes
    audio_text: str
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

    def align(self, media_url: str, transcript_text: str) -> AtaResponse:
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
            if type(body.get("code")) not in (int, str) or body["code"] not in (0, "0"):
                raise ProviderError(f"ATA submit failed with code {body.get('code')}")
            task_id = body.get("id")
            if not isinstance(task_id, str) or not task_id:
                raise ContractError("ATA submit returned no task id")
            metadata = ProviderMetadata(
                provider=self.provider,
                requested_model=self.model,
                resolved_model=self.model,
                response_id=task_id,
            )
            try:
                result = self._poll(client, appid, token, task_id)
            except httpx.RequestError as exc:
                raise DeliveryAmbiguousError(
                    "ATA query delivery/completion is uncertain", metadata=metadata
                ) from exc
            except (ProviderError, ContractError) as exc:
                raise type(exc)(str(exc), metadata=metadata) from exc
            completed_metadata = ProviderMetadata(
                provider=self.provider,
                requested_model=self.model,
                resolved_model=self.model,
                response_id=task_id,
                elapsed_ms=round((time.monotonic() - started) * 1000),
            )
            return AtaResponse(result, payload["audio_text"], completed_metadata)
        finally:
            if self._owns_client:
                client.close()
                self._client = None

    def _poll(
        self, client: httpx.Client, appid: str, token: str, task_id: str
    ) -> bytes:
        deadline = time.monotonic() + self._config.poll_timeout_seconds
        headers = {"Authorization": f"Bearer; {token}", "Resource-Id": ATA_RESOURCE_ID}
        params = {"appid": appid, "id": task_id, "blocking": "0"}
        while True:
            response = client.get(ATA_QUERY_URL, params=params, headers=headers)
            body = _checked_json(response, "ATA query")
            status = body.get("code")
            if type(status) in (int, str) and status in (0, "0"):
                return response.content
            if type(status) not in (int, str) or status not in (2000, "2000"):
                raise ProviderError(f"ATA task ended with status {status}")
            if time.monotonic() >= deadline:
                raise DeliveryAmbiguousError("ATA query timed out after task submission")
            time.sleep(self._config.poll_interval_seconds)

    def close(self) -> None:
        if self._owns_client and self._client is not None:
            self._client.close()
            self._client = None


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
