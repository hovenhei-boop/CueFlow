from __future__ import annotations

import base64
import gc
import json
import os
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from importlib import import_module
from pathlib import Path
from typing import Any, Protocol, cast

from cueflow.config import (
    ALIGNER_LANGUAGES,
    CLOUD_MODEL,
    LOCAL_ALIGNER_REPO,
    LOCAL_ALIGNER_REVISION,
    RuntimeConfig,
)
from cueflow.errors import (
    ContractError,
    DeliveryAmbiguousError,
    ProviderError,
    ProviderUnavailableError,
)


@dataclass(frozen=True)
class SemanticResult:
    source_text: str
    language: str | None
    response_id: str | None = None
    provider_uncertain_spans: tuple[Mapping[str, Any], ...] = ()
    semantic_confidence: Mapping[str, Any] | None = None


@dataclass(frozen=True)
class AlignmentToken:
    text: str
    local_start_ms: int
    local_end_ms: int
    confidence: Mapping[str, Any] | None = None


class SemanticTranscriber(Protocol):
    provider: str
    model: str
    revision: str

    def transcribe(
        self,
        audio_path: Path,
        glossary_terms: Sequence[str],
        *,
        rework_context: str | None = None,
    ) -> SemanticResult: ...

    def close(self) -> None: ...


class ForcedAligner(Protocol):
    provider: str
    model: str
    revision: str

    def align(self, audio_path: Path, text: str, language: str | None) -> list[AlignmentToken]: ...

    def close(self) -> None: ...


class LocalQwenForcedAligner:
    provider = "qwen-local"
    model = LOCAL_ALIGNER_REPO
    revision = LOCAL_ALIGNER_REVISION

    def __init__(self, runtime: RuntimeConfig) -> None:
        self.runtime = runtime
        self._model: Any | None = None

    def align(self, audio_path: Path, text: str, language: str | None) -> list[AlignmentToken]:
        if language not in ALIGNER_LANGUAGES:
            raise ContractError(
                "Forced Alignment requires a declared language supported by the pinned model"
            )
        model = self._load()
        try:
            results = model.align(audio=str(audio_path), text=text, language=language)
        except (MemoryError, RuntimeError, ValueError) as exc:
            raise ProviderUnavailableError(
                "local Qwen3-ForcedAligner inference failed; no fallback was applied"
            ) from exc
        if not isinstance(results, list) or len(results) != 1:
            raise ContractError("local aligner returned an invalid result count")
        tokens: list[AlignmentToken] = []
        for item in results[0]:
            token_text = getattr(item, "text", None)
            start_time = getattr(item, "start_time", None)
            end_time = getattr(item, "end_time", None)
            if not isinstance(token_text, str):
                raise ContractError("local aligner returned a token without text")
            if start_time is None or end_time is None:
                raise ContractError("local aligner returned a missing timestamp")
            try:
                start_ms = round(float(start_time) * 1000)
                end_ms = round(float(end_time) * 1000)
            except (TypeError, ValueError) as exc:
                raise ContractError("local aligner returned an invalid timestamp") from exc
            tokens.append(AlignmentToken(token_text, start_ms, end_ms))
        return tokens

    def _load(self) -> Any:
        if self._model is not None:
            return self._model
        try:
            torch = import_module("torch")
            qwen_asr = import_module("qwen_asr")
        except ImportError as exc:
            raise ProviderUnavailableError(
                "Forced Alignment requires the cueflow[alignment] dependencies"
            ) from exc
        snapshot = _snapshot_path(self.model, self.revision, self.runtime.model_cache)
        kwargs: dict[str, Any] = {
            "dtype": getattr(torch, self.runtime.device.dtype),
            "device_map": self.runtime.device.device,
        }
        try:
            self._model = qwen_asr.Qwen3ForcedAligner.from_pretrained(snapshot, **kwargs)
        except (MemoryError, OSError, RuntimeError) as exc:
            raise ProviderUnavailableError(
                "unable to load pinned Qwen3-ForcedAligner-0.6B with detected runtime capability"
            ) from exc
        return self._model

    def close(self) -> None:
        self._model = None
        gc.collect()
        _empty_cuda_cache()


class CloudOmniSemanticTranscriber:
    provider = "dashscope-openai-compatible"
    model = CLOUD_MODEL
    revision = CLOUD_MODEL

    def __init__(self, client_factory: Callable[..., Any] | None = None) -> None:
        self._client_factory = client_factory

    def transcribe(
        self,
        audio_path: Path,
        glossary_terms: Sequence[str],
        *,
        rework_context: str | None = None,
    ) -> SemanticResult:
        api_key = os.getenv("DASHSCOPE_API_KEY")
        base_url = os.getenv("DASHSCOPE_BASE_URL")
        if not api_key or not base_url:
            raise ProviderUnavailableError(
                "Remote provider requires DASHSCOPE_API_KEY and DASHSCOPE_BASE_URL"
            )
        factory = self._client_factory or _openai_factory()
        audio_data = "data:audio/wav;base64," + base64.b64encode(
            audio_path.read_bytes()
        ).decode("ascii")
        allowed_languages = ", ".join(ALIGNER_LANGUAGES)
        prompt = (
            "完整逐字转写音频中的实际人声。不得摘要、改写、润色、删除口语或擅自纠正。"
            "严格只返回 JSON 对象，字段只能是 source_text 和 language；source_text 是完整逐字正文，"
            f"language 必须从以下值中选择：{allowed_languages}。不要解释。\n"
            + _semantic_context(glossary_terms, rework_context)
        )
        try:
            client = factory(api_key=api_key, base_url=base_url)
        except Exception as exc:
            raise ProviderUnavailableError("cloud client could not be created") from exc
        try:
            response = client.chat.completions.create(
                model=self.model,
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "input_audio",
                                "input_audio": {"data": audio_data, "format": "wav"},
                            },
                            {"type": "text", "text": prompt},
                        ],
                    }
                ],
                modalities=["text"],
                stream=True,
                stream_options={"include_usage": True},
                temperature=0,
                response_format={"type": "json_object"},
            )
            response_text, response_id = collect_cloud_text_stream(response)
        except Exception as exc:
            if isinstance(exc, ContractError):
                raise
            if getattr(exc, "status_code", None) is not None:
                raise ProviderError(f"cloud provider explicit failure: {exc}") from exc
            raise DeliveryAmbiguousError(
                "cloud semantic request may have been delivered; automatic retry is forbidden"
            ) from exc
        text, language = parse_cloud_semantic_response(response_text)
        return SemanticResult(
            source_text=text,
            language=language,
            response_id=response_id,
            semantic_confidence=None,
        )

    def close(self) -> None:
        return None


def parse_cloud_semantic_response(text: str) -> tuple[str, str]:
    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ContractError("cloud semantic response must be strict JSON") from exc
    if not isinstance(value, dict) or set(value) != {"source_text", "language"}:
        raise ContractError("cloud semantic response may only contain source_text and language")
    source_text = value["source_text"]
    language = value["language"]
    if not isinstance(source_text, str) or not source_text.strip():
        raise ContractError("cloud semantic provider returned empty transcript text")
    if not isinstance(language, str) or language not in ALIGNER_LANGUAGES:
        raise ContractError("cloud semantic provider returned an unsupported alignment language")
    return source_text, language


def collect_cloud_text_stream(stream: Any) -> tuple[str, str | None]:
    parts: list[str] = []
    response_id: str | None = None
    try:
        for chunk in stream:
            chunk_id = getattr(chunk, "id", None)
            if chunk_id and response_id is None:
                response_id = str(chunk_id)
            choices = getattr(chunk, "choices", None)
            if not choices:
                continue
            content = getattr(choices[0].delta, "content", None)
            if isinstance(content, str):
                parts.append(content)
    except (AttributeError, IndexError, TypeError) as exc:
        raise ContractError("cloud provider returned an invalid streaming response") from exc
    text = "".join(parts)
    if not text:
        raise ContractError("cloud provider returned no text deltas")
    return text, response_id


def _semantic_context(terms: Sequence[str], rework_context: str | None) -> str:
    glossary = "、".join(terms)
    parts = [
        "以下专名/术语仅用于核对可能出现的发音，必须以实际音频为准，不能据此强制替换："
        + (glossary or "（无）")
    ]
    if rework_context:
        parts.append(rework_context)
    return "\n".join(parts)


def _openai_factory() -> Callable[..., Any]:
    try:
        module = import_module("openai")
    except ImportError as exc:
        raise ProviderUnavailableError(
            "Remote provider requires the cueflow[cloud] dependencies"
        ) from exc
    return cast(Callable[..., Any], module.OpenAI)


def _empty_cuda_cache() -> None:
    try:
        torch = import_module("torch")
    except ImportError:
        return
    if torch.cuda.is_available():
        torch.cuda.empty_cache()


def _snapshot_path(repo_id: str, revision: str, cache_dir: str | None) -> str:
    try:
        hub = import_module("huggingface_hub")
    except ImportError as exc:
        raise ProviderUnavailableError(
            "Forced Alignment requires huggingface_hub for pinned snapshot resolution"
        ) from exc
    try:
        return str(
            hub.snapshot_download(
                repo_id=repo_id,
                revision=revision,
                cache_dir=cache_dir,
            )
        )
    except Exception as exc:
        raise ProviderUnavailableError(
            f"unable to resolve pinned model snapshot {repo_id}@{revision}"
        ) from exc
