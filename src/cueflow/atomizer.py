from __future__ import annotations

import unicodedata
from collections.abc import Iterable, Mapping
from typing import Any

from cueflow.config import ATOMIZER_VERSION
from cueflow.errors import ContractError
from cueflow.schema import validate_transcript_payload


def is_cjk_character(character: str) -> bool:
    code = ord(character)
    return (
        0x3400 <= code <= 0x4DBF
        or 0x4E00 <= code <= 0x9FFF
        or 0xF900 <= code <= 0xFAFF
        or 0x20000 <= code <= 0x2EBEF
        or 0x30000 <= code <= 0x323AF
    )


def atomize(source_text: str) -> tuple[str, list[dict[str, Any]]]:
    if not isinstance(source_text, str):
        raise ContractError("source_text must be a string")
    leading = ""
    atoms: list[dict[str, Any]] = []
    decoration = ""
    index = 0

    def append_atom(text: str, atom_class: str) -> None:
        nonlocal decoration, leading
        if atoms:
            atoms[-1]["decoration_after"] += decoration
        else:
            leading += decoration
        decoration = ""
        atoms.append(
            {
                "atom_id": f"a{len(atoms) + 1:04d}",
                "position": len(atoms),
                "text": text,
                "atom_class": atom_class,
                "decoration_after": "",
            }
        )

    while index < len(source_text):
        character = source_text[index]
        category = unicodedata.category(character)
        if is_cjk_character(character):
            append_atom(character, "cjk_character")
            index += 1
            continue
        if category.startswith("L") or category.startswith("M"):
            end = index + 1
            while end < len(source_text):
                next_character = source_text[end]
                next_category = unicodedata.category(next_character)
                if (
                    next_character in "'’‐‑-"
                    and end + 1 < len(source_text)
                    and not is_cjk_character(source_text[end + 1])
                    and unicodedata.category(source_text[end + 1]).startswith(("L", "M"))
                ):
                    end += 1
                    continue
                if is_cjk_character(next_character) or not (
                    next_category.startswith("L") or next_category.startswith("M")
                ):
                    break
                end += 1
            append_atom(source_text[index:end], "word")
            index = end
            continue
        if category == "Nd":
            end = index + 1
            while end < len(source_text):
                next_character = source_text[end]
                if unicodedata.category(next_character) == "Nd":
                    end += 1
                    continue
                if (
                    next_character in ".,:/%"
                    and end + 1 < len(source_text)
                    and unicodedata.category(source_text[end + 1]) == "Nd"
                ):
                    end += 1
                    continue
                break
            append_atom(source_text[index:end], "number")
            index = end
            continue
        if category.startswith("S"):
            append_atom(character, "pronounceable_symbol")
            index += 1
            continue
        decoration += character
        index += 1

    if atoms:
        atoms[-1]["decoration_after"] += decoration
    else:
        leading += decoration
    return leading, atoms


def build_transcript_payload(
    *,
    source_text: str,
    base_asr_artifact_id: str,
    edit_resolution_artifact_id: str,
    correction_mode: str,
) -> dict[str, Any]:
    leading, atoms = atomize(source_text)
    payload: dict[str, Any] = {
        "source_text": source_text,
        "leading_decoration": leading,
        "atomizer_version": ATOMIZER_VERSION,
        "atoms": atoms,
        "base_asr_artifact_id": base_asr_artifact_id,
        "edit_resolution_artifact_id": edit_resolution_artifact_id,
        "correction_mode": correction_mode,
    }
    validate_transcript_payload(payload)
    return payload


def atom_signature(atoms: Iterable[Mapping[str, Any]]) -> tuple[tuple[str, str], ...]:
    return tuple((str(atom["atom_class"]), str(atom["text"])) for atom in atoms)


def normalized_atom_text(atom: Mapping[str, Any]) -> str:
    return unicodedata.normalize("NFC", str(atom["text"])).casefold()
