from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any

from cueflow.errors import ContractError


def parse_ata_result(raw_response: bytes) -> list[dict[str, Any]]:
    """Read only sentence text and integer milliseconds; do not judge their values."""
    try:
        value = json.loads(raw_response)
    except (ValueError, UnicodeError) as exc:
        raise ContractError("ATA result is not valid JSON") from exc
    if not isinstance(value, Mapping) or not isinstance(value.get("utterances"), list):
        raise ContractError("ATA result requires an utterances array")
    utterances: list[dict[str, Any]] = []
    for raw in value["utterances"]:
        if not isinstance(raw, Mapping) or not isinstance(raw.get("text"), str):
            raise ContractError("ATA utterance requires string text")
        start, end = raw.get("start_time"), raw.get("end_time")
        if type(start) is not int or type(end) is not int:
            raise ContractError("ATA utterance requires integer start_time/end_time")
        utterances.append({"text": raw["text"], "start_ms": start, "end_ms": end})
    return utterances


def ata_diagnostics(
    audio_text: str, utterances: Sequence[Mapping[str, Any]]
) -> dict[str, Any]:
    """A receipt, never an acceptance decision. Lengths are Python Unicode code points."""
    output = "".join(item["text"] for item in utterances)
    return {
        "utterance_count": len(utterances),
        "text_comparison": {
            "relation": "identical" if output == audio_text else "differs",
            "input_length": len(audio_text),
            "output_length": len(output),
            "length_delta": len(output) - len(audio_text),
        },
        "empty_text_count": sum(item["text"] == "" for item in utterances),
        "negative_time_count": sum(
            item["start_ms"] < 0 or item["end_ms"] < 0 for item in utterances
        ),
        "reversed_interval_count": sum(item["end_ms"] < item["start_ms"] for item in utterances),
        "zero_duration_count": sum(item["end_ms"] == item["start_ms"] for item in utterances),
        # Adjacent pairs in provider order with a non-empty interval intersection.
        "overlap_count": sum(
            max(left["start_ms"], right["start_ms"]) < min(left["end_ms"], right["end_ms"])
            for left, right in zip(utterances, utterances[1:], strict=False)
        ),
    }
