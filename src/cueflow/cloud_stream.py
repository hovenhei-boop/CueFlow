from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable, Mapping
from importlib import import_module
from typing import Any, cast

from cueflow.asr_contracts import ProviderMetadata
from cueflow.errors import (
    ContractError,
    DeliveryAmbiguousError,
    ProviderError,
    ProviderUnavailableError,
)


class CompletedResponseError(ContractError):
    """A paid, definitely completed response whose format is invalid."""

    def __init__(
        self,
        message: str,
        metadata: ProviderMetadata,
        *,
        raw_response: str | None = None,
        finish_reason: str | None = None,
    ) -> None:
        super().__init__(message)
        self.metadata = metadata
        self.raw_response = raw_response
        self.finish_reason = finish_reason

    def diagnostic(self) -> dict[str, Any]:
        result: dict[str, Any] = {"finish_reason": self.finish_reason}
        if self.raw_response is None:
            return result
        encoded = self.raw_response.encode("utf-8")
        result.update(
            {
                "raw_response": self.raw_response if len(encoded) <= 65_536 else None,
                "raw_response_sha256": "sha256:" + hashlib.sha256(encoded).hexdigest(),
                "raw_response_byte_length": len(encoded),
                "raw_response_truncated": len(encoded) > 65_536,
            }
        )
        return result


def strict_json(text: str) -> Any:
    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in items:
            if key in result:
                raise ContractError("Duplicate JSON key")
            result[key] = value
        return result

    try:
        return json.loads(
            text,
            object_pairs_hook=pairs,
            parse_constant=lambda value: (_ for _ in ()).throw(
                ContractError("Non-finite JSON constant")
            ),
        )
    except (ValueError, TypeError) as exc:
        raise ContractError("Response must be strict JSON") from exc


def complete_json(
    factory: Callable[..., Any],
    *,
    api_key: str,
    base_url: str,
    provider: str,
    model: str,
    body: Mapping[str, Any],
) -> tuple[Any, ProviderMetadata, str]:
    try:
        client = factory(api_key=api_key, base_url=base_url, max_retries=0, timeout=900.0)
    except Exception as exc:
        raise ProviderUnavailableError("Cloud client could not be created") from exc
    started = time.monotonic()
    parts: list[str] = []
    response_id: str | None = None
    resolved_model: str | None = None
    usage: Mapping[str, Any] | None = None
    search: list[Mapping[str, Any]] = []
    finish: str | None = None

    def current_metadata() -> ProviderMetadata:
        return ProviderMetadata(
            provider,
            model,
            resolved_model,
            response_id,
            round((time.monotonic() - started) * 1000),
            usage=usage,
            search_results=tuple(search),
        )

    try:
        stream = client.chat.completions.create(
            model=model, stream=True, stream_options={"include_usage": True}, **body
        )
        for chunk in stream:
            response_id = response_id or getattr(chunk, "id", None)
            resolved_model = getattr(chunk, "model", None) or resolved_model
            raw = getattr(chunk, "usage", None)
            if raw is not None:
                dumped = raw.model_dump() if hasattr(raw, "model_dump") else raw
                if isinstance(dumped, Mapping):
                    usage = dict(dumped)
            raw_search = getattr(chunk, "web_search", None)
            if isinstance(raw_search, list):
                for item in raw_search:
                    dumped = item.model_dump() if hasattr(item, "model_dump") else item
                    if isinstance(dumped, Mapping):
                        search.append(dict(dumped))
            choices = getattr(chunk, "choices", None)
            if choices:
                finish = getattr(choices[0], "finish_reason", None) or finish
                content = getattr(choices[0].delta, "content", None)
                if isinstance(content, str):
                    parts.append(content)
    except Exception as exc:
        metadata = current_metadata()
        if getattr(exc, "status_code", None) is not None:
            raise ProviderError(
                f"{provider} explicit HTTP failure: {getattr(exc, 'status_code', None)}",
                metadata=metadata,
            ) from exc
        raise DeliveryAmbiguousError(
            "Cloud stream delivery/completion is uncertain", metadata=metadata
        ) from exc
    finally:
        close = getattr(client, "close", None)
        if close is not None:
            close()
    metadata = current_metadata()
    if finish is None:
        raise DeliveryAmbiguousError(
            "Cloud stream ended without a completion marker", metadata=metadata
        )
    raw_text = "".join(parts)
    if finish != "stop":
        raise CompletedResponseError(
            "Cloud response was not a normal completion",
            metadata,
            raw_response=raw_text,
            finish_reason=finish,
        )
    try:
        return strict_json(raw_text), metadata, raw_text
    except ContractError as exc:
        raise CompletedResponseError(
            str(exc), metadata, raw_response=raw_text, finish_reason=finish
        ) from exc


def openai_factory() -> Callable[..., Any]:
    try:
        module = import_module("openai")
    except ImportError as exc:
        raise ProviderUnavailableError("Cloud providers require cueflow[cloud]") from exc
    return cast(Callable[..., Any], module.OpenAI)
