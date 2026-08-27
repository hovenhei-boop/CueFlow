from __future__ import annotations

import json
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from importlib import import_module
from typing import Any, Protocol, cast

from cueflow.config import LEXICON_MODEL
from cueflow.errors import (
    ContractError,
    DeliveryAmbiguousError,
    ProviderError,
    ProviderFormatError,
    ProviderIdentityError,
    ProviderPermissionError,
    ProviderUnavailableError,
)
from cueflow.term_candidates import CandidateOccurrence


@dataclass(frozen=True)
class LexiconExtractionRequest:
    evidence_artifact_id: str
    evidence_role: str
    units: tuple[Mapping[str, Any], ...]


@dataclass(frozen=True)
class LexiconExtractionResult:
    occurrences: tuple[CandidateOccurrence, ...]
    response_id: str | None
    provider_usage: Mapping[str, Any] | None
    provider_cost: float | None


class LexiconExtractor(Protocol):
    provider: str
    model: str

    def extract(self, request: LexiconExtractionRequest) -> LexiconExtractionResult: ...

    def close(self) -> None: ...


class CloudLexiconExtractor:
    provider = "dashscope-openai-compatible"
    model = LEXICON_MODEL

    def __init__(self, client_factory: Callable[..., Any] | None = None) -> None:
        self._client_factory = client_factory
        self._client: Any | None = None

    def preflight(self) -> None:
        self._get_client()

    def extract(self, request: LexiconExtractionRequest) -> LexiconExtractionResult:
        client = self._get_client()
        prompt = {
            "task": "extract terminology candidates from supplied Reference Evidence",
            "rules": [
                "Return JSON only with one top-level candidates array.",
                "Every candidate must quote an exact substring from one supplied unit.",
                "Offsets are zero-based half-open Unicode code point offsets within unit.text.",
                "Do not discard an identified proper noun because it appears only once.",
                "Do not translate, merge entities, or invent aliases.",
                "suggested_surface_form is optional and must remain separate from raw text.",
            ],
            "categories": ["proper_noun", "noun_or_term", "verb", "other"],
            "proper_noun_subtypes": [
                "person",
                "organization",
                "location",
                "event",
                "project_or_program",
                "product_brand_model_software",
                "standard_protocol_code",
                "work_or_title",
                "other",
            ],
            "evidence_artifact_id": request.evidence_artifact_id,
            "evidence_role": request.evidence_role,
            "units": [dict(unit) for unit in request.units],
        }
        try:
            response = client.chat.completions.create(
                model=self.model,
                messages=[
                    {
                        "role": "user",
                        "content": json.dumps(prompt, ensure_ascii=False, separators=(",", ":")),
                    }
                ],
                temperature=0,
                response_format={"type": "json_object"},
                stream=False,
            )
        except Exception as exc:
            _raise_sdk_provider_error(exc, "Lexicon extraction")
        text = _response_text(response)
        occurrences = _parse_occurrences(text)
        usage = _usage_mapping(getattr(response, "usage", None))
        return LexiconExtractionResult(
            occurrences=occurrences,
            response_id=_optional_string(getattr(response, "id", None)),
            provider_usage=usage,
            provider_cost=_provider_cost(usage),
        )

    def close(self) -> None:
        if self._client is None:
            return
        close = getattr(self._client, "close", None)
        if callable(close):
            close()
        self._client = None

    def _get_client(self) -> Any:
        if self._client is None:
            self._client = _cloud_client(self._client_factory)
        return self._client


def _parse_occurrences(text: str) -> tuple[CandidateOccurrence, ...]:
    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ContractError("Lexicon extractor returned invalid JSON") from exc
    if not isinstance(value, Mapping) or set(value) != {"candidates"}:
        raise ContractError("Lexicon extractor response must contain only candidates")
    raw_candidates = value["candidates"]
    if not isinstance(raw_candidates, list):
        raise ContractError("Lexicon extractor candidates must be an array")
    result: list[CandidateOccurrence] = []
    for raw in raw_candidates:
        if not isinstance(raw, Mapping):
            raise ContractError("Lexicon candidate must be an object")
        required = {
            "raw_surface_form",
            "field_path",
            "start_offset",
            "end_offset",
            "category",
            "proper_noun_subtype",
            "suggested_surface_form",
            "risk_tags",
        }
        if set(raw) != required:
            raise ContractError("Lexicon candidate fields do not match the current contract")
        field_path = raw["field_path"]
        risk_tags = raw["risk_tags"]
        if not isinstance(field_path, list) or any(
            isinstance(part, bool) or not isinstance(part, (str, int)) for part in field_path
        ):
            raise ContractError("Lexicon candidate field_path is invalid")
        if not isinstance(risk_tags, list) or any(
            not isinstance(tag, str) for tag in risk_tags
        ):
            raise ContractError("Lexicon candidate risk_tags are invalid")
        raw_surface = raw["raw_surface_form"]
        category = raw["category"]
        subtype = raw["proper_noun_subtype"]
        suggested = raw["suggested_surface_form"]
        if not isinstance(raw_surface, str) or not isinstance(category, str):
            raise ContractError("Lexicon candidate surface/category is invalid")
        if subtype is not None and not isinstance(subtype, str):
            raise ContractError("Lexicon candidate subtype is invalid")
        if suggested is not None and not isinstance(suggested, str):
            raise ContractError("Lexicon candidate suggested surface is invalid")
        result.append(
            CandidateOccurrence(
                raw_surface_form=raw_surface,
                field_path=tuple(field_path),
                start_offset=_integer(raw["start_offset"], "start_offset"),
                end_offset=_integer(raw["end_offset"], "end_offset"),
                category=category,
                proper_noun_subtype=subtype,
                suggested_surface_form=suggested,
                risk_tags=tuple(risk_tags),
            )
        )
    return tuple(result)


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
        raise ContractError("Lexicon provider response contains no text choice") from exc
    if not isinstance(content, str) or not content.strip():
        raise ContractError("Lexicon provider response contains empty text")
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
    for name in ("prompt_tokens", "completion_tokens", "total_tokens", "cost"):
        field = getattr(value, name, None)
        if field is not None:
            result[name] = field
    return result or None


def _provider_cost(usage: Mapping[str, Any] | None) -> float | None:
    if usage is None:
        return None
    value = usage.get("cost")
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        raise ContractError("provider usage cost is invalid")
    return float(value)


def _optional_string(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ContractError("optional provider response ID must be a string or null")
    return value


def _integer(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ContractError(f"Lexicon candidate {name} must be an integer")
    return cast(int, value)
