from __future__ import annotations

import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from cueflow.errors import ContractError
from cueflow.schema import ArtifactEnvelope

LEXICON_NORMALIZATION_VERSION = "0.1.0"
TERM_CATEGORIES = ("proper_noun", "noun_or_term", "verb", "other")
PROPER_NOUN_SUBTYPES = (
    "person",
    "organization",
    "location",
    "event",
    "project_or_program",
    "product_brand_model_software",
    "standard_protocol_code",
    "work_or_title",
    "other",
)
_CATEGORY_RANK = {value: index for index, value in enumerate(TERM_CATEGORIES)}
_SUBTYPE_RANK = {value: index for index, value in enumerate(PROPER_NOUN_SUBTYPES)}
_COORDINATE_FIELDS = (
    "page_number",
    "slide_number",
    "sheet_name",
    "start_ms",
    "end_ms",
    "part",
    "kind",
)


@dataclass(frozen=True)
class EvidenceUnit:
    field_path: tuple[str | int, ...]
    text: str
    coordinates: Mapping[str, Any]

    def as_manifest_item(self) -> dict[str, Any]:
        return {
            "field_path": list(self.field_path),
            "text": self.text,
            "coordinates": dict(self.coordinates),
        }


@dataclass(frozen=True)
class CandidateOccurrence:
    raw_surface_form: str
    field_path: tuple[str | int, ...]
    start_offset: int
    end_offset: int
    category: str
    proper_noun_subtype: str | None = None
    suggested_surface_form: str | None = None
    risk_tags: tuple[str, ...] = ()


@dataclass(frozen=True)
class ValidatedOccurrence:
    raw_surface_form: str
    normalized_surface_form: str
    field_path: tuple[str | int, ...]
    start_offset: int
    end_offset: int
    category: str
    proper_noun_subtype: str | None
    suggested_surface_form: str | None
    risk_tags: tuple[str, ...]
    context_before: str
    context_after: str
    coordinates: Mapping[str, Any]


def normalize_surface_form(value: str) -> str:
    if not isinstance(value, str):
        raise ContractError("term surface form must be a string")
    normalized = unicodedata.normalize("NFC", value).strip()
    if not normalized:
        raise ContractError("term surface form must not be empty after NFC + trim")
    return normalized


def evidence_units(evidence: ArtifactEnvelope) -> tuple[EvidenceUnit, ...]:
    if evidence.artifact_kind != "reference_evidence":
        raise ContractError("terminology discovery input must be Reference Evidence")
    content = evidence.payload.get("content")
    if isinstance(content, str):
        return (EvidenceUnit(("content",), content, {}),) if content else ()
    if not isinstance(content, Mapping):
        raise ContractError("Reference Evidence content cannot be mined for terms")

    blocks = content.get("blocks")
    if isinstance(blocks, list):
        units: list[EvidenceUnit] = []
        for index, raw in enumerate(blocks):
            if not isinstance(raw, Mapping):
                raise ContractError("Reference Evidence block must be an object")
            text = raw.get("text")
            if not isinstance(text, str):
                raise ContractError("Reference Evidence block.text must be a string")
            if text:
                units.append(
                    EvidenceUnit(
                        ("content", "blocks", index, "text"),
                        text,
                        _coordinates(raw),
                    )
                )
        return tuple(units)

    text = content.get("text")
    segments = content.get("segments")
    if isinstance(segments, list) and segments:
        units = []
        for index, raw in enumerate(segments):
            if not isinstance(raw, Mapping):
                raise ContractError("Reference Evidence segment must be an object")
            segment_text = raw.get("text")
            if not isinstance(segment_text, str):
                raise ContractError("Reference Evidence segment.text must be a string")
            if segment_text:
                units.append(
                    EvidenceUnit(
                        ("content", "segments", index, "text"),
                        segment_text,
                        _coordinates(raw),
                    )
                )
        if units:
            return tuple(units)
    if isinstance(text, str):
        return (EvidenceUnit(("content", "text"), text, {}),) if text else ()
    raise ContractError("Reference Evidence has no supported term-discovery text")


def validate_occurrence(
    occurrence: CandidateOccurrence,
    units: Sequence[EvidenceUnit],
    *,
    context_characters: int = 40,
) -> ValidatedOccurrence:
    category, subtype = validate_category(
        occurrence.category, occurrence.proper_noun_subtype
    )
    if (
        isinstance(occurrence.start_offset, bool)
        or isinstance(occurrence.end_offset, bool)
        or not isinstance(occurrence.start_offset, int)
        or not isinstance(occurrence.end_offset, int)
    ):
        raise ContractError("candidate offsets must be integers")
    matches = [unit for unit in units if unit.field_path == occurrence.field_path]
    if len(matches) != 1:
        raise ContractError("candidate field_path does not identify one Evidence text field")
    unit = matches[0]
    start = occurrence.start_offset
    end = occurrence.end_offset
    if start < 0 or end <= start or end > len(unit.text):
        raise ContractError("candidate offsets fall outside the referenced Evidence text")
    if unit.text[start:end] != occurrence.raw_surface_form:
        raise ContractError("candidate raw surface does not match its Evidence range")
    normalized = normalize_surface_form(occurrence.raw_surface_form)
    suggested = (
        normalize_surface_form(occurrence.suggested_surface_form)
        if occurrence.suggested_surface_form is not None
        else None
    )
    if any(not isinstance(tag, str) or not tag for tag in occurrence.risk_tags):
        raise ContractError("candidate risk tags must be non-empty strings")
    return ValidatedOccurrence(
        raw_surface_form=occurrence.raw_surface_form,
        normalized_surface_form=normalized,
        field_path=occurrence.field_path,
        start_offset=start,
        end_offset=end,
        category=category,
        proper_noun_subtype=subtype,
        suggested_surface_form=suggested,
        risk_tags=occurrence.risk_tags,
        context_before=unit.text[max(0, start - context_characters) : start],
        context_after=unit.text[end : end + context_characters],
        coordinates=dict(unit.coordinates),
    )


def validate_category(category: str, subtype: str | None) -> tuple[str, str | None]:
    if category not in _CATEGORY_RANK:
        raise ContractError(f"unsupported term category: {category}")
    if category == "proper_noun":
        if subtype not in _SUBTYPE_RANK:
            raise ContractError("proper_noun requires a supported subtype")
        return category, subtype
    if subtype is not None:
        raise ContractError("proper_noun_subtype is only valid for proper_noun")
    return category, None


def preferred_category(
    current_category: str,
    current_subtype: str | None,
    proposed_category: str,
    proposed_subtype: str | None,
) -> tuple[str, str | None]:
    current_category, current_subtype = validate_category(current_category, current_subtype)
    proposed_category, proposed_subtype = validate_category(
        proposed_category, proposed_subtype
    )
    if _CATEGORY_RANK[proposed_category] < _CATEGORY_RANK[current_category]:
        return proposed_category, proposed_subtype
    if proposed_category != current_category or proposed_category != "proper_noun":
        return current_category, current_subtype
    assert current_subtype is not None and proposed_subtype is not None
    if _SUBTYPE_RANK[proposed_subtype] < _SUBTYPE_RANK[current_subtype]:
        return current_category, proposed_subtype
    return current_category, current_subtype


def candidate_sort_key(value: Mapping[str, Any]) -> tuple[int, int, bytes, str]:
    category = str(value["display_category"])
    if category not in _CATEGORY_RANK:
        raise ContractError(f"unsupported term category: {category}")
    normalized = normalize_surface_form(str(value["normalized_surface_form"]))
    return (
        _CATEGORY_RANK[category],
        -len(normalized),
        normalized.encode("utf-8"),
        str(value["candidate_id"]),
    )


def _coordinates(value: Mapping[str, Any]) -> dict[str, Any]:
    return {field: value[field] for field in _COORDINATE_FIELDS if field in value}
