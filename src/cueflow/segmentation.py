from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from typing import Any

from cueflow.canonical import hash_json
from cueflow.config import SegmenterConfig
from cueflow.errors import ContractError
from cueflow.schema import ArtifactEnvelope, validate_subtitle_payload


@dataclass(frozen=True)
class AtomView:
    transcript_artifact_id: str
    atom_id: str
    position: int
    text: str
    atom_class: str
    decoration_after: str
    global_start_ms: int
    global_end_ms: int


def segment_subtitles(
    transcript: ArtifactEnvelope,
    alignment: ArtifactEnvelope,
    *,
    duration_ms: int,
    config: SegmenterConfig | None = None,
) -> dict[str, Any]:
    chosen = config or SegmenterConfig()
    atoms = flatten_aligned_atoms(transcript, alignment)
    cues: list[dict[str, Any]] = []
    cursor = 0
    while cursor < len(atoms):
        end = _choose_cue_end(atoms, cursor, chosen)
        cue_atoms = atoms[cursor:end]
        cues.append(
            {
                "cue_id": f"cue_{len(cues) + 1:05d}",
                "global_start_ms": cue_atoms[0].global_start_ms,
                "global_end_ms": cue_atoms[-1].global_end_ms,
                "atom_refs": [
                    {
                        "transcript_artifact_id": atom.transcript_artifact_id,
                        "atom_id": atom.atom_id,
                        "position": atom.position,
                        "text": atom.text,
                        "atom_class": atom.atom_class,
                        "decoration_after": atom.decoration_after,
                        "global_start_ms": atom.global_start_ms,
                        "global_end_ms": atom.global_end_ms,
                    }
                    for atom in cue_atoms
                ],
                "text": render_atom_text(cue_atoms, chosen),
                "display_unit_count": len(cue_atoms),
            }
        )
        cursor = end
    payload = {
        "duration_ms": duration_ms,
        "segmenter_config_hash": hash_json(asdict(chosen)),
        "transcript_artifact_id": transcript.artifact_id,
        "alignment_artifact_id": alignment.artifact_id,
        "cues": cues,
    }
    validate_subtitle_payload(payload)
    return payload


def flatten_aligned_atoms(
    transcript: ArtifactEnvelope, alignment: ArtifactEnvelope
) -> list[AtomView]:
    if alignment.payload.get("transcript_artifact_id") != transcript.artifact_id:
        raise ContractError("Alignment does not bind the supplied global Transcript")
    atoms = transcript.payload.get("atoms")
    assignments = alignment.payload.get("assignments")
    if not isinstance(atoms, list) or not isinstance(assignments, list):
        raise ContractError("Transcript or Alignment atoms are missing")
    if len(atoms) != len(assignments):
        raise ContractError("Transcript and Alignment lengths differ")
    result: list[AtomView] = []
    previous_end = -1
    for atom, assignment in zip(atoms, assignments, strict=True):
        if not isinstance(atom, Mapping) or not isinstance(assignment, Mapping):
            raise ContractError("Transcript or Alignment entry is invalid")
        if atom.get("atom_id") != assignment.get("atom_id"):
            raise ContractError("Transcript and Alignment atom identities differ")
        start = int(assignment["global_start_ms"])
        end = int(assignment["global_end_ms"])
        if start < previous_end:
            raise ContractError("global Alignment atoms overlap or are unordered")
        result.append(
            AtomView(
                transcript_artifact_id=transcript.artifact_id,
                atom_id=str(atom["atom_id"]),
                position=int(atom["position"]),
                text=str(atom["text"]),
                atom_class=str(atom["atom_class"]),
                decoration_after=str(atom.get("decoration_after", "")),
                global_start_ms=start,
                global_end_ms=end,
            )
        )
        previous_end = end
    return result


def render_atom_text(atoms: Sequence[AtomView], config: SegmenterConfig | None = None) -> str:
    chosen = config or SegmenterConfig()
    parts: list[str] = []
    for atom in atoms:
        parts.append(atom.text)
        parts.append(_style_decoration(atom.decoration_after, chosen))
    return "".join(parts).strip()


def _style_decoration(decoration: str, config: SegmenterConfig) -> str:
    result: list[str] = []
    for character in decoration:
        if character in config.comma_punctuation:
            if not result or result[-1] != " ":
                result.append(" ")
        elif character in config.removable_punctuation:
            continue
        elif character.isspace():
            if not result or result[-1] != " ":
                result.append(" ")
        else:
            result.append(character)
    return "".join(result)


def _choose_cue_end(atoms: Sequence[AtomView], start: int, config: SegmenterConfig) -> int:
    capacity_end = min(len(atoms), start + config.max_display_units)
    terminal = [
        index + 1
        for index in range(start, capacity_end)
        if _is_terminal(atoms[index].decoration_after)
    ]
    if terminal:
        return terminal[0]
    if capacity_end == len(atoms):
        return capacity_end
    punctuation = [
        index + 1
        for index in range(start, capacity_end)
        if _is_punctuation_boundary(atoms[index].decoration_after)
    ]
    if punctuation:
        return punctuation[-1]
    clause_starts = [
        index
        for index in range(start + 1, capacity_end)
        if atoms[index].atom_class == "word"
        and atoms[index].text.casefold() in config.english_clause_starters
    ]
    return clause_starts[-1] if clause_starts else capacity_end


def _is_terminal(decoration: str) -> bool:
    return any(character in "。；;？?！!." for character in decoration)


def _is_punctuation_boundary(decoration: str) -> bool:
    return any(character in "，,、：:" for character in decoration)
