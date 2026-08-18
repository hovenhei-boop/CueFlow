from __future__ import annotations

import unicodedata
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from cueflow.atomizer import atomize, normalized_atom_text
from cueflow.config import GLOSSARY_NORMALIZATION_VERSION
from cueflow.errors import ContractError
from cueflow.schema import validate_glossary_payload


def normalize_terms(terms: Iterable[str]) -> list[str]:
    normalized: set[str] = set()
    for term in terms:
        if not isinstance(term, str):
            raise ContractError("glossary terms must be strings")
        value = unicodedata.normalize("NFC", term).strip()
        if not value:
            raise ContractError("glossary terms must not be empty after normalization")
        normalized.add(value)
    return sorted(normalized)


def glossary_payload(terms: Iterable[str]) -> dict[str, Any]:
    payload = {
        "terms": normalize_terms(terms),
        "normalization_version": GLOSSARY_NORMALIZATION_VERSION,
    }
    validate_glossary_payload(payload)
    return payload


def effective_glossary(
    system_glossary: Mapping[str, Any], project_glossary: Mapping[str, Any]
) -> dict[str, Any]:
    system_terms = system_glossary.get("terms")
    project_terms = project_glossary.get("terms")
    if not isinstance(system_terms, list) or not isinstance(project_terms, list):
        raise ContractError("glossary payloads require terms arrays")
    return glossary_payload([*system_terms, *project_terms])


def glossary_atom_sequences(terms: Sequence[str]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for term in terms:
        _, atoms = atomize(term)
        result.append(
            {
                "term": term,
                "atoms": atoms,
                "normalized": [normalized_atom_text(atom) for atom in atoms],
                "classes": [str(atom["atom_class"]) for atom in atoms],
            }
        )
    return result


def exact_protected_spans(
    transcript_atoms: Sequence[Mapping[str, Any]], terms: Sequence[str]
) -> list[tuple[int, int, str]]:
    spans: list[tuple[int, int, str]] = []
    for term_info in glossary_atom_sequences(terms):
        term_atoms = term_info["atoms"]
        if len(term_atoms) < 2:
            continue
        length = len(term_atoms)
        for start in range(0, len(transcript_atoms) - length + 1):
            window = transcript_atoms[start : start + length]
            if [str(atom["atom_class"]) for atom in window] != term_info["classes"]:
                continue
            if [normalized_atom_text(atom) for atom in window] == term_info["normalized"]:
                spans.append((start, start + length, str(term_info["term"])))
    return spans
