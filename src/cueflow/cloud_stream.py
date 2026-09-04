from __future__ import annotations

from collections.abc import Callable
from importlib import import_module
from typing import Any, cast

from cueflow.errors import ContractError, ProviderUnavailableError


def collect_text_stream(stream: Any) -> tuple[str, str | None, dict[str, Any] | None]:
    parts: list[str] = []
    response_id: str | None = None
    usage: dict[str, Any] | None = None
    try:
        for chunk in stream:
            chunk_id = getattr(chunk, "id", None)
            if chunk_id and response_id is None:
                response_id = str(chunk_id)
            raw_usage = getattr(chunk, "usage", None)
            if raw_usage is not None:
                if hasattr(raw_usage, "model_dump"):
                    dumped = raw_usage.model_dump()
                    if isinstance(dumped, dict):
                        usage = dumped
                elif isinstance(raw_usage, dict):
                    usage = dict(raw_usage)
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
    return text, response_id, usage


def openai_factory() -> Callable[..., Any]:
    try:
        module = import_module("openai")
    except ImportError as exc:
        raise ProviderUnavailableError(
            "DashScope providers require the cueflow[cloud] dependencies"
        ) from exc
    return cast(Callable[..., Any], module.OpenAI)
