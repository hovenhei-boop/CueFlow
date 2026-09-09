from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from importlib.resources import files
from typing import Any, Protocol

from cueflow.asr_contracts import ProviderMetadata
from cueflow.cloud_stream import CompletedResponseError, complete_json, openai_factory, strict_json
from cueflow.config import KIMI_CORRECTION_MODEL, QWEN_CORRECTION_MODEL
from cueflow.errors import (
    ContractError,
    ProviderUnavailableError,
)

PROMPT_VERSION = "transcript-recovery-fulltext-zh-v1"
PROMPT_RESOURCE = "prompts/transcript_recovery_fulltext_zh_v1.txt"


@dataclass(frozen=True)
class CorrectionRequest:
    base_text: str
    peer_text: str
    references: tuple[Mapping[str, Any], ...]
    user_keywords: tuple[str, ...]
    comparison_hunks: tuple[Mapping[str, Any], ...]


@dataclass(frozen=True)
class CorrectionResult:
    corrected_text: str
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
        value, metadata, raw_text = complete_json(
            self._client_factory or openai_factory(),
            api_key=api_key,
            base_url=base_url,
            provider=self.provider,
            model=self.model,
            body={
                "messages": [{"role": "user", "content": _multimodal_content(request)}],
                "temperature": 1 if self.arm == "kimi" else 0,
                "response_format": {"type": "json_object"},
                "extra_body": self._search_extra_body(),
            },
        )
        try:
            text = _parse_correction_value(value)
        except ContractError as exc:
            raise CompletedResponseError(
                str(exc), metadata, raw_response=raw_text, finish_reason="stop"
            ) from exc
        return CorrectionResult(text, metadata)

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


def _parse_correction_value(value: Any) -> str:
    if not isinstance(value, Mapping) or set(value) != {"corrected_text"}:
        raise ContractError("Correction response must contain only corrected_text")
    text = value["corrected_text"]
    if not isinstance(text, str) or not text:
        raise ContractError("corrected_text must be a non-empty string")
    return text


def parse_correction_response(text: str) -> str:
    return _parse_correction_value(strict_json(text))


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
