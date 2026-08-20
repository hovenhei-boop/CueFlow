from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from cueflow.atomizer import atomize, normalized_atom_text
from cueflow.errors import ContractError
from cueflow.providers import AlignmentToken
from cueflow.schema import validate_alignment_against, validate_alignment_payload


def build_alignment_payload(
    *,
    media_chunk_artifact_id: str,
    media_chunk: Mapping[str, Any],
    transcript_artifact_id: str,
    transcript: Mapping[str, Any],
    tokens: Sequence[AlignmentToken],
) -> dict[str, Any]:
    if media_chunk.get("chunk_id") != transcript.get("chunk_id"):
        raise ContractError("cannot align mismatched media chunk and transcript")
    atoms = transcript.get("atoms")
    if not isinstance(atoms, list):
        raise ContractError("transcript atoms are missing")
    chunk_start = int(media_chunk["global_start_ms"])
    chunk_end = int(media_chunk["global_end_ms"])
    chunk_duration = chunk_end - chunk_start
    assignments: list[dict[str, Any]] = []
    token_atoms = [_single_provider_atom(item.text) for item in tokens]
    exact_shape = len(token_atoms) == len(atoms) and all(item is not None for item in token_atoms)
    exact_text = exact_shape and all(
        normalized_atom_text(provider_atom) == normalized_atom_text(transcript_atom)
        and provider_atom["atom_class"] == transcript_atom["atom_class"]
        for provider_atom, transcript_atom in zip(token_atoms, atoms, strict=True)
        if provider_atom is not None
    )
    if not exact_text:
        assignments = [
            {
                "atom_id": str(atom["atom_id"]),
                "status": "unaligned",
                "reason": "provider_token_sequence_mismatch",
            }
            for atom in atoms
        ]
    else:
        previous_end = -1
        for atom, token in zip(atoms, tokens, strict=True):
            local_start = token.local_start_ms
            local_end = token.local_end_ms
            valid = (
                local_start >= 0
                and local_end > local_start
                and local_start >= previous_end
                and local_end <= chunk_duration
            )
            if not valid:
                assignments.append(
                    {
                        "atom_id": str(atom["atom_id"]),
                        "status": "unaligned",
                        "reason": "provider_timestamp_out_of_chunk",
                    }
                )
                continue
            global_start = chunk_start + local_start
            global_end = chunk_start + local_end
            assignment: dict[str, Any] = {
                "atom_id": str(atom["atom_id"]),
                "status": "aligned",
                "global_start_ms": global_start,
                "global_end_ms": global_end,
            }
            if token.confidence is not None:
                assignment["acoustic_confidence"] = dict(token.confidence)
            assignments.append(assignment)
            previous_end = local_end
    payload = {
        "chunk_id": str(media_chunk["chunk_id"]),
        "media_chunk_artifact_id": media_chunk_artifact_id,
        "transcript_artifact_id": transcript_artifact_id,
        "provider_coordinate_system": "chunk_local_milliseconds",
        "global_offset_applied_once_ms": chunk_start,
        "assignments": assignments,
    }
    validate_alignment_payload(payload)
    validate_alignment_against(payload, transcript, media_chunk)
    return payload


def _single_provider_atom(text: str) -> Mapping[str, Any] | None:
    _, atoms = atomize(text)
    return atoms[0] if len(atoms) == 1 else None
