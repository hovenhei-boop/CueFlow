from __future__ import annotations

import hashlib
import json
import os
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from importlib.resources import files
from typing import Any, Protocol

from cueflow.asr_contracts import ProviderMetadata
from cueflow.cloud_stream import openai_factory
from cueflow.config import KIMI_CORRECTION_MODEL, QWEN_CORRECTION_MODEL
from cueflow.edit_resolution import Edit, parse_edits_json
from cueflow.errors import (
    ContractError,
    DeliveryAmbiguousError,
    ProviderError,
    ProviderUnavailableError,
)

PROMPT_VERSION = "transcript-recovery-edits-zh-v2"
PROMPT_RESOURCE = "prompts/transcript_recovery_edits_zh_v2.txt"


@dataclass(frozen=True)
class CorrectionRequest:
    base_text: str
    peer_text: str
    references: tuple[Mapping[str, Any], ...]
    user_keywords: tuple[str, ...]
    comparison_hunks: tuple[Mapping[str, Any], ...]


@dataclass(frozen=True)
class CorrectionResult:
    edits: tuple[Edit, ...]
    metadata: ProviderMetadata


class CorrectionProvider(Protocol):
    arm: str
    provider: str
    model: str

    def correct(self, request: CorrectionRequest) -> CorrectionResult: ...

    def close(self) -> None: ...


class OpenAiCompatibleCorrectionProvider:
    arm: str
    provider: str
    model: str
    api_key_env: str
    base_url_env: str

    def __init__(self, client_factory: Callable[..., Any] | None = None) -> None:
        self._client_factory = client_factory

    def correct(self, request: CorrectionRequest) -> CorrectionResult:
        if not request.base_text:
            raise ContractError("Correction requires a non-empty Frozen BaseTranscript")
        api_key = os.getenv(self.api_key_env)
        base_url = os.getenv(self.base_url_env)
        if not api_key or not base_url:
            raise ProviderUnavailableError(
                f"{self.arm} Correction requires {self.api_key_env} and {self.base_url_env}"
            )
        factory = self._client_factory or openai_factory()
        try:
            client = factory(api_key=api_key, base_url=base_url, max_retries=0)
        except Exception as exc:
            raise ProviderUnavailableError(
                f"{self.arm} Correction client could not be created"
            ) from exc
        content = _multimodal_content(request)
        started = time.monotonic()
        first_contract_error: ContractError | None = None
        for attempt in range(2):
            try:
                stream = client.chat.completions.create(
                    model=self.model,
                    messages=[{"role": "user", "content": content}],
                    stream=True,
                    stream_options={"include_usage": True},
                    temperature=0,
                    response_format={"type": "json_object"},
                    extra_body=self._search_extra_body(),
                )
                text, response_id, resolved_model, usage = _collect_stream(stream)
            except Exception as exc:
                if isinstance(exc, ContractError):
                    if attempt == 0:
                        first_contract_error = exc
                        continue
                    raise
                if getattr(exc, "status_code", None) is not None:
                    raise ProviderError(f"{self.arm} Correction explicit failure: {exc}") from exc
                raise DeliveryAmbiguousError(
                    f"{self.arm} Correction may have been delivered; automatic retry is forbidden"
                ) from exc
            try:
                edits = parse_correction_response(text)
            except ContractError as exc:
                if attempt == 0:
                    first_contract_error = exc
                    continue
                raise ContractError(
                    f"{self.arm} Correction returned invalid strict JSON twice"
                ) from first_contract_error
            return CorrectionResult(
                edits,
                ProviderMetadata(
                    provider=self.provider,
                    requested_model=self.model,
                    resolved_model=resolved_model,
                    response_id=response_id,
                    elapsed_ms=round((time.monotonic() - started) * 1000),
                    usage=usage,
                ),
            )
        raise AssertionError("unreachable")

    def _search_extra_body(self) -> Mapping[str, Any]:
        raise NotImplementedError

    def close(self) -> None:
        return None


class QwenCorrectionProvider(OpenAiCompatibleCorrectionProvider):
    arm = "qwen"
    provider = "dashscope-openai-compatible"
    model = QWEN_CORRECTION_MODEL
    api_key_env = "DASHSCOPE_API_KEY"
    base_url_env = "DASHSCOPE_BASE_URL"

    def _search_extra_body(self) -> Mapping[str, Any]:
        return {
            "enable_search": True,
            "search_options": {"forced_search": True, "search_strategy": "max"},
        }


class KimiCorrectionProvider(OpenAiCompatibleCorrectionProvider):
    arm = "kimi"
    provider = "moonshot-openai-compatible"
    model = KIMI_CORRECTION_MODEL
    api_key_env = "MOONSHOT_API_KEY"
    base_url_env = "MOONSHOT_BASE_URL"

    def _search_extra_body(self) -> Mapping[str, Any]:
        return {"enable_search": True}


def load_correction_prompt() -> tuple[str, str]:
    prompt = files("cueflow").joinpath(PROMPT_RESOURCE).read_text(encoding="utf-8")
    return prompt, "sha256:" + hashlib.sha256(prompt.encode("utf-8")).hexdigest()


def parse_correction_response(text: str) -> tuple[Edit, ...]:
    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ContractError("Correction response must be strict JSON") from exc
    return parse_edits_json(value)


def _multimodal_content(request: CorrectionRequest) -> list[dict[str, Any]]:
    content: list[dict[str, Any]] = []
    text_references: list[str] = []
    for reference in request.references:
        kind = reference.get("kind")
        if kind == "pdf_url":
            content.append({"type": "file", "file": {"file_url": str(reference["url"])}})
        elif kind == "image_url":
            content.append({"type": "image_url", "image_url": {"url": str(reference["url"])}})
        elif kind == "text":
            text_references.append(f"[{reference['display_name']}]\n{reference['text']}")
        else:
            raise ContractError("Correction received an unsupported Reference kind")
    prompt, _ = load_correction_prompt()
    payload = {
        "Frozen BaseTranscript": request.base_text,
        "Independent PeerTranscript": request.peer_text,
        "Plain-text References": text_references,
        "User Keywords": list(request.user_keywords),
        "Mechanical ASR differences": list(request.comparison_hunks),
    }
    content.append(
        {
            "type": "text",
            "text": prompt
            + "\n\n以下是本次 CueFlow 输入：\n"
            + json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        }
    )
    return content


def _collect_stream(
    stream: Any,
) -> tuple[str, str | None, str | None, Mapping[str, Any] | None]:
    parts: list[str] = []
    response_id: str | None = None
    resolved_model: str | None = None
    usage: Mapping[str, Any] | None = None
    finish_reason: str | None = None
    try:
        for chunk in stream:
            if response_id is None and getattr(chunk, "id", None):
                response_id = str(chunk.id)
            if getattr(chunk, "model", None):
                resolved_model = str(chunk.model)
            raw_usage = getattr(chunk, "usage", None)
            if raw_usage is not None:
                dumped = raw_usage.model_dump() if hasattr(raw_usage, "model_dump") else raw_usage
                if isinstance(dumped, Mapping):
                    usage = dict(dumped)
            choices = getattr(chunk, "choices", None)
            if choices:
                reason = getattr(choices[0], "finish_reason", None)
                if reason:
                    finish_reason = str(reason)
                value = getattr(choices[0].delta, "content", None)
                if isinstance(value, str):
                    parts.append(value)
    except (AttributeError, IndexError, TypeError) as exc:
        raise DeliveryAmbiguousError(
            "Correction stream could not be read to a complete response"
        ) from exc
    text = "".join(parts)
    if finish_reason is None:
        raise DeliveryAmbiguousError("Correction stream ended without a completion marker")
    if finish_reason != "stop":
        raise ContractError("Correction response was not a complete normal completion")
    if not text:
        raise ContractError("Correction provider returned no text")
    return text, response_id, resolved_model, usage
