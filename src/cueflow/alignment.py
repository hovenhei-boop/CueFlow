from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from cueflow.asr_contracts import TimedUnit
from cueflow.atomizer import atomize, normalized_atom_text
from cueflow.errors import ContractError
from cueflow.schema import validate_alignment_against, validate_alignment_payload


def build_alignment_payload(
    *,
    media_object_artifact_id: str,
    timeline_audio_artifact_id: str,
    duration_ms: int,
    transcript_artifact_id: str,
    transcript: Mapping[str, Any],
    tokens: Sequence[TimedUnit],
) -> dict[str, Any]:
    atoms = transcript.get("atoms")
    if not isinstance(atoms, list) or not atoms:
        raise ContractError("Transcript atoms are missing")
    provider_atoms = _expand_provider_tokens(tokens, duration_ms)
    if len(provider_atoms) != len(atoms):
        raise ContractError("ATA token sequence does not exactly cover corrected Transcript atoms")
    assignments: list[dict[str, Any]] = []
    for transcript_atom, provider_atom in zip(atoms, provider_atoms, strict=True):
        if not isinstance(transcript_atom, Mapping):
            raise ContractError("Transcript atom is invalid")
        provider_shape, start_ms, end_ms = provider_atom
        if provider_shape["atom_class"] != transcript_atom.get(
            "atom_class"
        ) or normalized_atom_text(provider_shape) != normalized_atom_text(transcript_atom):
            raise ContractError("ATA token text differs from corrected Transcript atoms")
        assignments.append(
            {
                "atom_id": str(transcript_atom["atom_id"]),
                "global_start_ms": start_ms,
                "global_end_ms": end_ms,
            }
        )
    payload = {
        "transcript_artifact_id": transcript_artifact_id,
        "media_object_artifact_id": media_object_artifact_id,
        "timeline_audio_artifact_id": timeline_audio_artifact_id,
        "duration_ms": duration_ms,
        "provider_coordinate_system": "global_milliseconds",
        "assignments": assignments,
    }
    validate_alignment_payload(payload)
    validate_alignment_against(payload, transcript, duration_ms)
    return payload


def _expand_provider_tokens(
    tokens: Sequence[TimedUnit], duration_ms: int
) -> list[tuple[Mapping[str, Any], int, int]]:
    expanded: list[tuple[Mapping[str, Any], int, int]] = []
    previous_end = -1
    for token in tokens:
        if (
            token.start_ms < previous_end
            or token.end_ms <= token.start_ms
            or token.end_ms > duration_ms
        ):
            raise ContractError("ATA returned invalid global word timing")
        _, shapes = atomize(token.text)
        if not shapes:
            previous_end = token.end_ms
            continue
        duration = token.end_ms - token.start_ms
        if duration < len(shapes):
            raise ContractError("ATA word interval is too short for its text atoms")
        boundaries = [
            token.start_ms + round(duration * index / len(shapes))
            for index in range(len(shapes) + 1)
        ]
        expanded.extend(
            (shape, boundaries[index], boundaries[index + 1]) for index, shape in enumerate(shapes)
        )
        previous_end = token.end_ms
    if not expanded:
        raise ContractError("ATA returned no pronounceable tokens")
    return expanded
