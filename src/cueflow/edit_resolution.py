from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from cueflow.errors import ContractError


def apply_resolved_payload(base_text: str, edits: Sequence[Mapping[str, Any]]) -> str:
    result = base_text
    previous_start = len(base_text) + 1
    for raw in sorted(edits, key=lambda item: int(item["start"]), reverse=True):
        start, end = int(raw["start"]), int(raw["end"])
        if (
            not 0 <= start <= end <= len(base_text)
            or end > previous_start
            or start == previous_start
        ):
            raise ContractError("resolved edits overlap or have an invalid Base interval")
        if base_text[start:end] != raw["original"]:
            raise ContractError("resolved edit no longer matches frozen Base")
        result = result[:start] + str(raw["replacement"]) + result[end:]
        previous_start = start
    return result
