from __future__ import annotations

import difflib
import unicodedata
from collections.abc import Sequence
from typing import Any

from cueflow.asr_contracts import TimedUnit

SENTENCE_PUNCTUATION = frozenset("，,。.！!？?；;：:")


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
