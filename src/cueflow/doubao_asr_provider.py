from __future__ import annotations

import json
import os
import re
import time
import uuid
from collections.abc import Mapping, Sequence
from typing import Any
from urllib.parse import quote

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
DOUBAO_RESOURCE_ID = "volc.seedasr.auc"


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
            metadata = ProviderMetadata(
                provider=self.provider,
                requested_model=self.model,
                resolved_model=self.model,
                response_id=task_id,
            )
            try:
                result = self._poll(client, headers)
                text, units = parse_doubao_result(result)
            except httpx.RequestError as exc:
                raise DeliveryAmbiguousError(
                    "Doubao ASR query is uncertain after task submission",
                    metadata=metadata,
                ) from exc
            except ProviderError as exc:
                if exc.metadata is None:
                    exc.metadata = metadata
                raise
            except ContractError as exc:
                exc.metadata = metadata
                raise
            metadata = ProviderMetadata(
                provider=self.provider,
                requested_model=self.model,
                resolved_model=self.model,
                response_id=task_id,
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
            if not response.is_success:
                _validate_doubao_response(response, "Doubao ASR query")
            status = response.headers.get("X-Api-Status-Code")
            if status == "20000000":
                return _json_object(response, "Doubao ASR query")
            if status not in {"20000001", "20000002", "20000003"}:
                _validate_doubao_response(response, "Doubao ASR query")
            if time.monotonic() >= deadline:
                raise DeliveryAmbiguousError("Doubao ASR query timed out after task submission")
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
    if not response.is_success:
        detail = _doubao_http_error_detail(response)
        raise ProviderError(f"{operation} failed with HTTP {response.status_code}: {detail}")
    status = response.headers.get("X-Api-Status-Code")
    if status is not None and status != "20000000":
        message = response.headers.get("X-Api-Message", "unknown error")
        raise ProviderError(f"{operation} failed: {status} {message}")


def _doubao_http_error_detail(response: httpx.Response) -> str:
    # Only non-2xx Doubao diagnostics use this local redaction path.
    secrets = {
        value
        for name in (
            "DOUBAO_API_KEY",
            "DOUBAO_APP_KEY",
            "DOUBAO_ACCESS_KEY",
            "DASHSCOPE_API_KEY",
            "MOONSHOT_API_KEY",
            "ZHIPU_API_KEY",
            "TOS_ACCESS_KEY",
            "TOS_SECRET_KEY",
            "VOLCENGINE_ATA_ACCESS_TOKEN",
        )
        if (value := os.getenv(name))
    }
    try:
        request_headers: Mapping[str, str] = response.request.headers
    except RuntimeError:
        request_headers = {}
    for name in ("X-Api-Key", "X-Api-App-Key", "X-Api-Access-Key", "Authorization"):
        if value := request_headers.get(name):
            secrets.add(value)
            if name == "Authorization":
                secrets.add(value.rsplit(" ", 1)[-1])
    tokens = {
        token
        for secret in secrets
        for token in (secret, quote(secret, safe=""), json.dumps(secret)[1:-1])
    }

    def redact(text: str) -> str:
        for token in sorted(tokens, key=len, reverse=True):
            text = text.replace(token, "[REDACTED]")
        # Match credential field names exactly, never diagnostic X-Api-* fields.
        return re.sub(
            r"""(?i)(?<![\w-])((?:x-api-(?:app-|access-)?key|authorization|"""
            r"""(?:[\w]+_)?api_key|tos_(?:access|secret)_key|"""
            r"""x-tos-(?:credential|signature|security-token)|ossaccesskeyid|signature)"""
            r"""["']?\s*[:=]\s*)(?:"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|"""
            r"""(?:Bearer[;]?\s+|Basic\s+)?(?:\[REDACTED\]|[^\s,;&}\]"']+))""",
            lambda match: match[1] + "[REDACTED]",
            text,
        )

    body = redact(response.content.decode("utf-8", errors="replace"))
    detail: dict[str, Any] = {"http_status": response.status_code}
    for name in ("X-Api-Status-Code", "X-Api-Message", "X-Tt-Logid"):
        value = response.headers.get(name)
        detail[name] = redact(value) if value is not None else None
    detail.update(body=body[:4096], body_truncated=len(body) > 4096)
    return json.dumps(detail, ensure_ascii=False)


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
