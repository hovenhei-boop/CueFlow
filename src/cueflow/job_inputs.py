from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from cueflow.config import MAX_USER_KEYWORDS
from cueflow.errors import ContractError

OFFICE_FORMATS = frozenset({"doc", "docx", "ppt", "pptx", "xls", "xlsx"})


@dataclass(frozen=True)
class ReferenceSpec:
    kind: str
    value: str


def build_job_input_payload(
    *,
    source_asset_id: str,
    references: Sequence[ReferenceSpec] = (),
    keywords: Sequence[str] = (),
) -> dict[str, object]:
    if references:
        raise ContractError("Reference files must be captured in their owning Run")
    return {
        "source_asset_id": source_asset_id,
        "references": [],
        "user_keywords": normalize_keywords(keywords),
    }


def normalize_keywords(values: Sequence[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for raw in values:
        if not isinstance(raw, str):
            raise ContractError("keywords must be strings")
        value = raw.strip()
        if not value:
            raise ContractError("keywords must not be empty or whitespace-only")
        if value not in seen:
            seen.add(value)
            result.append(value)
    if len(result) > MAX_USER_KEYWORDS:
        raise ContractError(f"at most {MAX_USER_KEYWORDS} user keywords are allowed")
    return result
