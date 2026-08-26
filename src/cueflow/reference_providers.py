from __future__ import annotations

import base64
import json
import os
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from importlib import import_module
from pathlib import Path
from typing import Any, Protocol, cast
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from cueflow.config import (
    CLOUD_DOCUMENT_MODEL,
    CLOUD_REFERENCE_ASR_MODEL,
    REFERENCE_ASR_SEGMENT_MAX_MS,
    REFERENCE_AUDIO_CHANNELS,
    REFERENCE_AUDIO_SAMPLE_RATE_HZ,
    REFERENCE_DOCUMENT_POLL_INTERVAL_SECONDS,
    REFERENCE_DOCUMENT_POLL_TIMEOUT_SECONDS,
    REFERENCE_VISION_FPS,
    REFERENCE_VISION_HEIGHT,
    REFERENCE_VISION_JPEG_QV,
    REFERENCE_VISION_MODEL,
    REFERENCE_VISION_WINDOW_MS,
)
from cueflow.errors import (
    ContractError,
    DeliveryAmbiguousError,
    ProviderCleanupError,
    ProviderError,
    ProviderFormatError,
    ProviderIdentityError,
    ProviderPermissionError,
    ProviderUnavailableError,
)

ASR_ENDPOINT = "https://dashscope.aliyuncs.com/api/v1/services/aigc/multimodal-generation/generation"


@dataclass(frozen=True)
class ReferenceAsrRequest:
    audio_path: Path
    source_start_ms: int
    source_end_ms: int


@dataclass(frozen=True)
class ReferenceVisionRequest:
    image_paths: tuple[Path, ...]
    evidence_role: str
    manifest: Mapping[str, Any]


@dataclass(frozen=True)
class CloudDocumentRequest:
    path: Path
    detected_format: str


@dataclass(frozen=True)
class ReferenceModelResult:
    text: str
    segments: tuple[dict[str, Any], ...]
    response_id: str | None
    provider_usage: Mapping[str, Any] | None
    provider_usage_duration: float | None
    provider_cost: float | None


class ReferenceAsrProvider(Protocol):
    provider: str
    model: str

    def transcribe(self, request: ReferenceAsrRequest) -> ReferenceModelResult: ...

    def close(self) -> None: ...


class ReferenceVisionProvider(Protocol):
    provider: str
    model: str

    def recognize(self, request: ReferenceVisionRequest) -> ReferenceModelResult: ...

    def close(self) -> None: ...


class CloudDocumentProvider(Protocol):
    provider: str
    model: str
    last_remote_file_id: str | None
    last_cleanup_status: str | None
    last_result: ReferenceModelResult | None

    def parse(self, request: CloudDocumentRequest) -> ReferenceModelResult: ...

    def close(self) -> None: ...


class CloudReferenceAsr:
    provider = "dashscope-multimodal-generation"
    model = CLOUD_REFERENCE_ASR_MODEL

    def __init__(
        self,
        *,
        post_json: Callable[[str, Mapping[str, str], bytes, float], tuple[int, bytes]]
        | None = None,
    ) -> None:
        self._post_json = post_json or _post_json

    def transcribe(self, request: ReferenceAsrRequest) -> ReferenceModelResult:
        api_key = os.getenv("DASHSCOPE_API_KEY")
        if not api_key:
            raise ProviderIdentityError("Remote provider requires DASHSCOPE_API_KEY")
        encoded = base64.b64encode(request.audio_path.read_bytes()).decode("ascii")
        payload = {
            "model": self.model,
            "input": {
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "input_audio",
                                "input_audio": {"data": f"data:audio/wav;base64,{encoded}"},
                            }
                        ],
                    }
                ]
            },
            "parameters": {
                "format": "wav",
                "sample_rate": REFERENCE_AUDIO_SAMPLE_RATE_HZ,
                "asr_options": {"enable_itn": False},
            },
        }
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        try:
            status, response_body = self._post_json(
                ASR_ENDPOINT,
                {
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                    "X-DashScope-SSE": "disable",
                },
                body,
                300.0,
            )
        except (ProviderError, ContractError):
            raise
        except Exception as exc:
            raise DeliveryAmbiguousError(
                "Cloud Reference ASR delivery outcome is ambiguous"
            ) from exc
        value = _json_object(response_body, "Cloud Reference ASR response")
        if status != 200:
            _raise_http_provider_error(status, value, "Cloud Reference ASR")
        output = value.get("output")
        output_map = output if isinstance(output, Mapping) else {}
        text = output_map.get("text")
        if not isinstance(text, str) or not text.strip():
            raise ContractError("Cloud Reference ASR returned no text")
        usage_value = value.get("usage")
        usage = dict(usage_value) if isinstance(usage_value, Mapping) else None
        provider_duration = _nullable_float(usage.get("duration")) if usage else None
        segments = _rebase_segments(
            output_map.get("segments"),
            text,
            request.source_start_ms,
            request.source_end_ms,
        )
        return ReferenceModelResult(
            text=text,
            segments=segments,
            response_id=_optional_string(value.get("request_id") or value.get("id")),
            provider_usage=usage,
            provider_usage_duration=provider_duration,
            provider_cost=_provider_cost(usage),
        )

    def preflight(self) -> None:
        if not os.getenv("DASHSCOPE_API_KEY"):
            raise ProviderIdentityError("Remote provider requires DASHSCOPE_API_KEY")

    def close(self) -> None:
        return None


class CloudReferenceVision:
    provider = "dashscope-openai-compatible"
    model = REFERENCE_VISION_MODEL

    def __init__(self, client_factory: Callable[..., Any] | None = None) -> None:
        self._client_factory = client_factory

    def recognize(self, request: ReferenceVisionRequest) -> ReferenceModelResult:
        client = _cloud_client(self._client_factory)
        content: list[dict[str, Any]] = [
            {
                "type": "text",
                "text": (
                    "Extract visible reference text verbatim. Preserve reading order "
                    "and line breaks. "
                    "Do not summarize, translate, infer missing text, or return frame indexes."
                ),
            }
        ]
        for path in request.image_paths:
            mime = "image/png" if path.suffix.lower() == ".png" else "image/jpeg"
            encoded = base64.b64encode(path.read_bytes()).decode("ascii")
            content.append(
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:{mime};base64,{encoded}"},
                }
            )
        try:
            response = client.chat.completions.create(
                model=self.model,
                messages=[{"role": "user", "content": content}],
                temperature=0,
                stream=False,
            )
        except Exception as exc:
            _raise_sdk_provider_error(exc, "Reference Vision")
        response_text = _response_text(response)
        usage = _usage_mapping(getattr(response, "usage", None))
        return ReferenceModelResult(
            text=response_text,
            segments=(),
            response_id=_optional_string(getattr(response, "id", None)),
            provider_usage=usage,
            provider_usage_duration=None,
            provider_cost=_provider_cost(usage),
        )

    def preflight(self) -> None:
        _cloud_client(self._client_factory)

    def close(self) -> None:
        return None


class QwenCloudDocumentParser:
    provider = "dashscope-openai-compatible"
    model = CLOUD_DOCUMENT_MODEL

    def __init__(
        self,
        client_factory: Callable[..., Any] | None = None,
        *,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._client_factory = client_factory
        self._sleep = sleep
        self._monotonic = monotonic
        self.last_remote_file_id: str | None = None
        self.last_cleanup_status: str | None = None
        self.last_result: ReferenceModelResult | None = None

    def parse(self, request: CloudDocumentRequest) -> ReferenceModelResult:
        if request.detected_format not in {"doc", "ppt", "xls", "pdf"}:
            raise ContractError("Cloud document parse accepts legacy Office or PDF only")
        client = _cloud_client(self._client_factory)
        file_id: str | None = None
        primary_error: BaseException | None = None
        cleanup_error: BaseException | None = None
        result: ReferenceModelResult | None = None
        self.last_remote_file_id = None
        self.last_cleanup_status = None
        self.last_result = None
        try:
            with request.path.open("rb") as stream:
                uploaded = client.files.create(file=stream, purpose="file-extract")
            file_id = _optional_string(getattr(uploaded, "id", None))
            if file_id is None:
                raise ContractError("Cloud document upload returned no file_id")
            self.last_remote_file_id = file_id
            self.last_cleanup_status = "pending"
            self._wait_until_processed(client, file_id)
            response = client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": "You extract text from reference materials."},
                    {"role": "system", "content": f"fileid://{file_id}"},
                    {
                        "role": "user",
                        "content": (
                            "Return the document text verbatim in reading order. "
                            "Do not summarize, explain, translate, or infer missing text."
                        ),
                    },
                ],
                stream=False,
            )
            text = _response_text(response)
            usage = _usage_mapping(getattr(response, "usage", None))
            result = ReferenceModelResult(
                text=text,
                segments=(),
                response_id=_optional_string(getattr(response, "id", None)),
                provider_usage=usage,
                provider_usage_duration=None,
                provider_cost=_provider_cost(usage),
            )
            self.last_result = result
        except BaseException as exc:
            primary_error = exc
        finally:
            if file_id is not None:
                try:
                    deleted = client.files.delete(file_id)
                    deleted_value = getattr(deleted, "deleted", None)
                    if deleted_value is not True:
                        raise ProviderCleanupError(
                            f"Cloud document file_id was not confirmed deleted: {file_id}"
                        )
                    self.last_cleanup_status = "deleted"
                except BaseException as cleanup_exc:
                    self.last_cleanup_status = "delete_failed"
                    cleanup_error = cleanup_exc
        if primary_error is not None:
            if isinstance(primary_error, (ContractError, ProviderError)):
                raise primary_error
            _raise_sdk_provider_error(primary_error, "Cloud document parse")
        if cleanup_error is not None:
            raise ProviderCleanupError(
                f"Cloud document file_id cleanup failed: {file_id}"
            ) from cleanup_error
        if result is None:
            raise ContractError("Cloud document parse produced no result")
        return result

    def preflight(self) -> None:
        _cloud_client(self._client_factory)

    def _wait_until_processed(self, client: Any, file_id: str) -> None:
        deadline = self._monotonic() + REFERENCE_DOCUMENT_POLL_TIMEOUT_SECONDS
        while True:
            record = client.files.retrieve(file_id)
            status = str(getattr(record, "status", "")).lower()
            if status in {"processed", "ready", "succeeded"}:
                return
            if status in {"error", "failed", "cancelled"}:
                raise ProviderFormatError(
                    f"Cloud document provider rejected uploaded format: {status}"
                )
            if self._monotonic() >= deadline:
                raise ProviderError("Cloud document parse polling timed out")
            self._sleep(REFERENCE_DOCUMENT_POLL_INTERVAL_SECONDS)

    def close(self) -> None:
        return None


def reference_vision_actual_config(evidence_role: str) -> dict[str, Any]:
    result: dict[str, Any] = {
        "model": REFERENCE_VISION_MODEL,
        "evidence_role": evidence_role,
    }
    if evidence_role == "burned_subtitle":
        result.update(
            {
                "height": REFERENCE_VISION_HEIGHT,
                "fps": REFERENCE_VISION_FPS,
                "jpeg_qv": REFERENCE_VISION_JPEG_QV,
                "window_ms": REFERENCE_VISION_WINDOW_MS,
            }
        )
    elif evidence_role == "image_visual":
        result.update(
            {
                "height": REFERENCE_VISION_HEIGHT,
                "jpeg_qv": REFERENCE_VISION_JPEG_QV,
            }
        )
    elif evidence_role == "bitmap_subtitle":
        result.update(
            {
                "input": "unique_bitmap_png",
                "dedupe": "raw_pixel_bytes",
                "occurrence_timing": "pipeline_manifest",
            }
        )
    else:
        raise ContractError(f"unsupported Reference Vision evidence role: {evidence_role}")
    return result


def cloud_asr_actual_config() -> dict[str, Any]:
    return {
        "model": CLOUD_REFERENCE_ASR_MODEL,
        "format": "wav",
        "codec": "pcm_s16le",
        "channels": REFERENCE_AUDIO_CHANNELS,
        "sample_rate": REFERENCE_AUDIO_SAMPLE_RATE_HZ,
        "segment_max_ms": REFERENCE_ASR_SEGMENT_MAX_MS,
        "asr_options": {"enable_itn": False},
    }


def cloud_document_actual_config() -> dict[str, Any]:
    return {
        "model": CLOUD_DOCUMENT_MODEL,
        "purpose": "file-extract",
        "poll_interval_seconds": REFERENCE_DOCUMENT_POLL_INTERVAL_SECONDS,
        "poll_timeout_seconds": REFERENCE_DOCUMENT_POLL_TIMEOUT_SECONDS,
        "delete_file_id_in_finally": True,
    }


def _cloud_client(client_factory: Callable[..., Any] | None) -> Any:
    api_key = os.getenv("DASHSCOPE_API_KEY")
    base_url = os.getenv("DASHSCOPE_BASE_URL")
    if not api_key:
        raise ProviderIdentityError("Remote provider requires DASHSCOPE_API_KEY")
    if not base_url:
        raise ProviderUnavailableError("Remote provider requires DASHSCOPE_BASE_URL")
    factory = client_factory or _openai_factory()
    try:
        return factory(api_key=api_key, base_url=base_url)
    except Exception as exc:
        raise ProviderUnavailableError("cloud client could not be created") from exc


def _openai_factory() -> Callable[..., Any]:
    try:
        module = import_module("openai")
    except ImportError as exc:
        raise ProviderUnavailableError("Remote provider requires cueflow[cloud]") from exc
    return cast(Callable[..., Any], module.OpenAI)


def _post_json(
    url: str,
    headers: Mapping[str, str],
    body: bytes,
    timeout: float,
) -> tuple[int, bytes]:
    request = Request(url, data=body, headers=dict(headers), method="POST")
    try:
        with urlopen(request, timeout=timeout) as response:
            return int(response.status), response.read()
    except HTTPError as exc:
        return int(exc.code), exc.read()
    except URLError as exc:
        raise DeliveryAmbiguousError("provider delivery outcome is ambiguous") from exc


def _raise_http_provider_error(status: int, body: Mapping[str, Any], operation: str) -> None:
    message = json.dumps(body, ensure_ascii=False)
    if status == 401:
        raise ProviderIdentityError(f"{operation} identity error: {message}")
    if status == 403:
        raise ProviderPermissionError(f"{operation} permission error: {message}")
    if status in {400, 415, 422}:
        raise ProviderFormatError(f"{operation} format/provider error: {message}")
    raise ProviderError(f"{operation} provider error HTTP {status}: {message}")


def _raise_sdk_provider_error(exc: BaseException, operation: str) -> None:
    status = getattr(exc, "status_code", None)
    if status == 401:
        raise ProviderIdentityError(f"{operation} identity error: {exc}") from exc
    if status == 403:
        raise ProviderPermissionError(f"{operation} permission error: {exc}") from exc
    if status in {400, 415, 422}:
        raise ProviderFormatError(f"{operation} format/provider error: {exc}") from exc
    if status is not None:
        raise ProviderError(f"{operation} provider error: {exc}") from exc
    raise DeliveryAmbiguousError(f"{operation} delivery outcome is ambiguous") from exc


def _response_text(response: Any) -> str:
    try:
        content = response.choices[0].message.content
    except (AttributeError, IndexError, TypeError) as exc:
        raise ContractError("provider response contains no text choice") from exc
    if not isinstance(content, str) or not content.strip():
        raise ContractError("provider response contains empty text")
    return content


def _usage_mapping(value: Any) -> Mapping[str, Any] | None:
    if value is None:
        return None
    if isinstance(value, Mapping):
        return dict(value)
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        dumped = model_dump()
        if isinstance(dumped, Mapping):
            return dict(dumped)
    result: dict[str, Any] = {}
    for name in ("prompt_tokens", "completion_tokens", "total_tokens", "duration", "cost"):
        field = getattr(value, name, None)
        if field is not None:
            result[name] = field
    return result or None


def _provider_cost(usage: Mapping[str, Any] | None) -> float | None:
    if usage is None:
        return None
    return _nullable_float(usage.get("cost"))


def _rebase_segments(
    raw_segments: Any,
    text: str,
    source_start_ms: int,
    source_end_ms: int,
) -> tuple[dict[str, Any], ...]:
    if not isinstance(raw_segments, Sequence) or isinstance(raw_segments, (str, bytes)):
        return (
            {"start_ms": source_start_ms, "end_ms": source_end_ms, "text": text},
        )
    result: list[dict[str, Any]] = []
    for raw in raw_segments:
        if not isinstance(raw, Mapping):
            raise ContractError("Reference ASR segment is not an object")
        start = _nullable_float(raw.get("start") or raw.get("start_time"))
        end = _nullable_float(raw.get("end") or raw.get("end_time"))
        segment_text = raw.get("text")
        if start is None or end is None or end <= start or not isinstance(segment_text, str):
            raise ContractError("Reference ASR segment timing is invalid")
        rebased_start = source_start_ms + round(start * 1000)
        rebased_end = source_start_ms + round(end * 1000)
        if rebased_start < source_start_ms or rebased_end > source_end_ms:
            raise ContractError("Reference ASR segment lies outside its local input interval")
        result.append(
            {"start_ms": rebased_start, "end_ms": rebased_end, "text": segment_text}
        )
    return tuple(result)


def _json_object(value: bytes, name: str) -> Mapping[str, Any]:
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ContractError(f"{name} is not JSON") from exc
    if not isinstance(parsed, Mapping):
        raise ContractError(f"{name} is not an object")
    return cast(Mapping[str, Any], parsed)


def _nullable_float(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise ContractError("provider numeric field is invalid")
    try:
        return float(value)
    except ValueError as exc:
        raise ContractError("provider numeric field is invalid") from exc


def _optional_string(value: Any) -> str | None:
    return str(value) if value is not None else None
