from __future__ import annotations

import os
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import httpx

from cueflow.asr_contracts import AsrResult, ProviderMetadata, TimedUnit
from cueflow.config import GLM_ASR_MODEL
from cueflow.errors import (
    ContractError,
    DeliveryAmbiguousError,
    ProviderError,
    ProviderUnavailableError,
)

GLM_ASR_URL = "https://open.bigmodel.cn/api/paas/v4/audio/transcriptions"


def build_glm_form(user_keywords: Sequence[str]) -> dict[str, Any]:
    result: dict[str, Any] = {"model": GLM_ASR_MODEL, "stream": "false"}
    if user_keywords:
        result["hotwords"] = list(user_keywords)
    return result


class GlmEvidenceAsrProvider:
    provider = "zhipu-asr"
    model = GLM_ASR_MODEL

    def __init__(self, client: httpx.Client | None = None) -> None:
        self._client = client
        self._owns_client = client is None

    def transcribe(self, audio_path: Path, *, user_keywords: Sequence[str]) -> AsrResult:
        api_key = os.getenv("ZHIPU_API_KEY")
        if not api_key:
            raise ProviderUnavailableError("GLM ASR requires ZHIPU_API_KEY")
        if audio_path.stat().st_size > 25_000_000:
            raise ContractError("GLM evidence window exceeds 25 MB")
        client = self._client or httpx.Client(timeout=60.0)
        self._client = client
        started = time.monotonic()
        try:
            try:
                with audio_path.open("rb") as stream:
                    response = client.post(
                        GLM_ASR_URL,
                        headers={"Authorization": f"Bearer {api_key}"},
                        data=build_glm_form(user_keywords),
                        files={"file": (audio_path.name, stream, "audio/wav")},
                    )
            except httpx.RequestError as exc:
                raise DeliveryAmbiguousError(
                    "GLM ASR request may have been delivered; automatic retry is forbidden"
                ) from exc
            if response.is_error:
                raise ProviderError(f"GLM ASR failed with HTTP {response.status_code}")
            body = _json_object(response)
            text = body.get("text")
            if not isinstance(text, str) or not text:
                raise ContractError("GLM ASR returned no transcript text")
            segments = body.get("segments")
            units: list[TimedUnit] = []
            if isinstance(segments, list):
                for raw in segments:
                    item = _object(raw, "GLM segment")
                    unit_text = item.get("text")
                    start = item.get("start", item.get("start_time"))
                    end = item.get("end", item.get("end_time"))
                    if (
                        isinstance(unit_text, str)
                        and unit_text
                        and isinstance(start, (int, float))
                        and isinstance(end, (int, float))
                    ):
                        start_ms = round(float(start) * 1000) if isinstance(start, float) else start
                        end_ms = round(float(end) * 1000) if isinstance(end, float) else end
                        if end_ms > start_ms >= 0:
                            units.append(TimedUnit(unit_text, start_ms, end_ms))
            metadata = ProviderMetadata(
                provider=self.provider,
                requested_model=self.model,
                resolved_model=(
                    body.get("model") if isinstance(body.get("model"), str) else self.model
                ),
                response_id=(body.get("id") if isinstance(body.get("id"), str) else None),
                elapsed_ms=round((time.monotonic() - started) * 1000),
                usage=(body.get("usage") if isinstance(body.get("usage"), Mapping) else None),
            )
            return AsrResult(text, tuple(units), metadata)
        finally:
            if self._owns_client:
                client.close()
                self._client = None

    def close(self) -> None:
        if self._owns_client and self._client is not None:
            self._client.close()
            self._client = None


def _json_object(response: httpx.Response) -> Mapping[str, Any]:
    try:
        return _object(response.json(), "GLM ASR response")
    except ValueError as exc:
        raise ContractError("GLM ASR returned invalid JSON") from exc


def _object(value: Any, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ContractError(f"{name} must be an object")
    return value
