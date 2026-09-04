from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True)
class TimedUnit:
    text: str
    start_ms: int
    end_ms: int
    confidence: Mapping[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "text": self.text,
            "start_ms": self.start_ms,
            "end_ms": self.end_ms,
        }
        if self.confidence is not None:
            result["confidence"] = dict(self.confidence)
        return result


@dataclass(frozen=True)
class ProviderMetadata:
    provider: str
    requested_model: str
    resolved_model: str | None = None
    response_id: str | None = None
    elapsed_ms: int | None = None
    reasoning_ms: int | None = None
    usage: Mapping[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "requested_model": self.requested_model,
            "resolved_model": self.resolved_model,
            "response_id": self.response_id,
            "elapsed_ms": self.elapsed_ms,
            "reasoning_ms": self.reasoning_ms,
            "usage": dict(self.usage) if self.usage is not None else None,
        }


@dataclass(frozen=True)
class AsrResult:
    source_text: str
    timed_units: tuple[TimedUnit, ...]
    metadata: ProviderMetadata


class WholeFileAsrProvider(Protocol):
    provider: str
    model: str

    def transcribe(self, media_url: str, *, user_keywords: Sequence[str]) -> AsrResult: ...

    def close(self) -> None: ...
