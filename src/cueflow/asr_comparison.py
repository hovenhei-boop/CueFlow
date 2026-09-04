from __future__ import annotations

import difflib
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cueflow.asr_contracts import TimedUnit
from cueflow.config import EvidenceWindowConfig
from cueflow.errors import ContractError
from cueflow.media import slice_wave

SENTENCE_PUNCTUATION = frozenset("，,。.！!？?；;：:")


@dataclass(frozen=True)
class EvidenceWindow:
    window_id: str
    start_ms: int
    end_ms: int
    disagreement_ids: tuple[str, ...]


def compare_asr(
    base_text: str,
    peer_text: str,
    base_units: Sequence[TimedUnit],
    peer_units: Sequence[TimedUnit],
) -> list[dict[str, Any]]:
    # Diagnostic text comparison must not depend on audio-time mapping.
    del base_units, peer_units
    matcher = difflib.SequenceMatcher(a=base_text, b=peer_text, autojunk=False)
    hunks: list[dict[str, Any]] = []
    for tag, a0, a1, b0, b1 in matcher.get_opcodes():
        if tag == "equal":
            continue
        base_fragment = base_text[a0:a1]
        peer_fragment = peer_text[b0:b1]
        category = classify_hunk(base_text, peer_text, a0, a1, b0, b1)
        hunks.append(
            {
                "base_start": a0,
                "base_end": a1,
                "peer_start": b0,
                "peer_end": b1,
                "base_text": base_fragment,
                "peer_text": peer_fragment,
                "category": category,
            }
        )
    return hunks


def classify_hunk(base_text: str, peer_text: str, a0: int, a1: int, b0: int, b1: int) -> str:
    fragments = base_text[a0:a1] + peer_text[b0:b1]
    if not fragments or any(
        character not in SENTENCE_PUNCTUATION and not character.isspace() for character in fragments
    ):
        return "lexical"
    if _connects_lexical_token(base_text, a0, a1) or _connects_lexical_token(peer_text, b0, b1):
        return "lexical"
    return "prosodic_format_only"


def extract_evidence_window(
    timeline_wav: Path,
    destination: Path,
    window: EvidenceWindow,
    config: EvidenceWindowConfig | None = None,
) -> int:
    chosen = config or EvidenceWindowConfig()
    if window.end_ms - window.start_ms > chosen.max_duration_ms:
        raise ContractError("GLM evidence window exceeds 30 seconds before extraction")
    slice_wave(timeline_wav, destination, window.start_ms, window.end_ms)
    byte_length = destination.stat().st_size
    if byte_length > chosen.max_bytes:
        raise ContractError("GLM evidence window exceeds 25 MB after extraction")
    return byte_length


class TimedTextIndex:
    def __init__(self, text: str, units: Sequence[TimedUnit]) -> None:
        if not text or not units:
            raise ContractError("ASR text and timed units are required")
        spans: list[tuple[int, int, TimedUnit]] = []
        cursor = 0
        for unit in units:
            position = text.find(unit.text, cursor)
            if position < 0:
                raise ContractError("ASR timed-unit text does not occur in its transcript")
            spans.append((position, position + len(unit.text), unit))
            cursor = position + len(unit.text)
        self._text = text
        self._spans = spans

    def interval(self, start: int, end: int) -> tuple[int, int] | None:
        if start == end:
            position = min(start, max(0, len(self._text) - 1))
            overlapping = [item for item in self._spans if item[0] <= position < item[1]]
            if not overlapping and position > 0:
                overlapping = [item for item in self._spans if item[0] <= position - 1 < item[1]]
        else:
            overlapping = [item for item in self._spans if item[0] < end and item[1] > start]
        if not overlapping:
            return None
        if start != end:
            covered_until = start
            for unit_start, unit_end, _ in overlapping:
                if unit_start > covered_until:
                    return None
                covered_until = max(covered_until, unit_end)
            if covered_until < end:
                return None
        return min(item[2].start_ms for item in overlapping), max(
            item[2].end_ms for item in overlapping
        )


def _connects_lexical_token(text: str, start: int, end: int) -> bool:
    left = text[start - 1] if start > 0 else None
    right = text[end] if end < len(text) else None
    if right is not None and _is_lexical(right) and text[start:end].endswith("."):
        # Leading identifier dots (.NET) are not safe sentence formatting either.
        return True
    return left is not None and right is not None and _is_lexical(left) and _is_lexical(right)


def _is_lexical(character: str) -> bool:
    category = unicodedata.category(character)
    return category.startswith(("L", "M", "N")) and not _is_cjk(character)


def _is_cjk(character: str) -> bool:
    code = ord(character)
    return (
        0x3400 <= code <= 0x4DBF
        or 0x4E00 <= code <= 0x9FFF
        or 0xF900 <= code <= 0xFAFF
        or 0x20000 <= code <= 0x2EBEF
        or 0x30000 <= code <= 0x323AF
    )
