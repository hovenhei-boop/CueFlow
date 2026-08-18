from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from typing import Any

from cueflow.canonical import hash_json
from cueflow.config import SegmenterConfig
from cueflow.errors import ContractError
from cueflow.glossary import exact_protected_spans
from cueflow.schema import ArtifactEnvelope, validate_subtitle_payload


@dataclass(frozen=True)
class AtomView:
    transcript_artifact_id: str
    chunk_id: str
    atom_id: str
    position: int
    text: str
    atom_class: str
    decoration_after: str
    global_start_ms: int
    global_end_ms: int

    def atom_mapping(self) -> dict[str, Any]:
        return {
            "atom_id": self.atom_id,
            "position": self.position,
            "text": self.text,
            "atom_class": self.atom_class,
            "decoration_after": self.decoration_after,
        }


def segment_subtitles(
    transcripts: Sequence[ArtifactEnvelope],
    alignments: Sequence[ArtifactEnvelope],
    glossary_terms: Sequence[str],
    *,
    duration_ms: int,
    config: SegmenterConfig | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    chosen = config or SegmenterConfig()
    atoms = flatten_aligned_atoms(transcripts, alignments)
    protected = _protected_units(atoms, glossary_terms)
    cues: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    cursor = 0
    while cursor < len(atoms):
        end, overflow, protected_term = _choose_cue_end(atoms, cursor, protected, chosen)
        cue_atoms = atoms[cursor:end]
        cue_id = f"cue_{len(cues) + 1:05d}"
        cue = {
            "cue_id": cue_id,
            "global_start_ms": cue_atoms[0].global_start_ms,
            "global_end_ms": cue_atoms[-1].global_end_ms,
            "atom_spans": _atom_spans(cue_atoms),
            "atom_refs": [
                {
                    "transcript_artifact_id": atom.transcript_artifact_id,
                    "chunk_id": atom.chunk_id,
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
            "protected_overflow": overflow,
        }
        cues.append(cue)
        if overflow:
            warnings.append(
                {
                    "severity": "warning",
                    "code": "protected_unit_exceeds_display_limit",
                    "resolution_status": "unresolved",
                    "locations": [{"cue_id": cue_id}],
                    "observed": {
                        "term": protected_term,
                        "display_unit_count": len(cue_atoms),
                        "limit": chosen.max_display_units,
                    },
                }
            )
        cursor = end
    payload = {
        "duration_ms": duration_ms,
        "segmenter_config_hash": hash_json(asdict(chosen)),
        "transcript_artifact_ids": [item.artifact_id for item in transcripts],
        "alignment_artifact_ids": [item.artifact_id for item in alignments],
        "cues": cues,
    }
    validate_subtitle_payload(payload)
    return payload, warnings


def flatten_aligned_atoms(
    transcripts: Sequence[ArtifactEnvelope], alignments: Sequence[ArtifactEnvelope]
) -> list[AtomView]:
    alignment_by_transcript = {
        str(item.payload.get("transcript_artifact_id")): item for item in alignments
    }
    result: list[AtomView] = []
    for transcript in transcripts:
        alignment = alignment_by_transcript.get(transcript.artifact_id)
        if alignment is None:
            raise ContractError("Subtitle is missing an Alignment for a current Transcript")
        atoms = transcript.payload.get("atoms")
        assignments = alignment.payload.get("assignments")
        if not isinstance(atoms, list) or not isinstance(assignments, list):
            raise ContractError("Transcript or Alignment atoms are missing")
        if len(atoms) != len(assignments):
            raise ContractError("Transcript and Alignment lengths differ")
        for atom, assignment in zip(atoms, assignments, strict=True):
            if not isinstance(atom, dict) or not isinstance(assignment, dict):
                raise ContractError("Transcript or Alignment entry is invalid")
            if atom.get("atom_id") != assignment.get("atom_id"):
                raise ContractError("Transcript and Alignment atom identities differ")
            if assignment.get("status") != "aligned":
                raise ContractError("Subtitle cannot be built from an unaligned real-sound Atom")
            result.append(
                AtomView(
                    transcript_artifact_id=transcript.artifact_id,
                    chunk_id=str(transcript.payload["chunk_id"]),
                    atom_id=str(atom["atom_id"]),
                    position=int(atom["position"]),
                    text=str(atom["text"]),
                    atom_class=str(atom["atom_class"]),
                    decoration_after=str(atom.get("decoration_after", "")),
                    global_start_ms=int(assignment["global_start_ms"]),
                    global_end_ms=int(assignment["global_end_ms"]),
                )
            )
    previous_end = -1
    for atom in result:
        if atom.global_start_ms < previous_end:
            raise ContractError("global Alignment atoms overlap or are unordered")
        previous_end = atom.global_end_ms
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


def _protected_units(
    atoms: Sequence[AtomView], glossary_terms: Sequence[str]
) -> dict[int, tuple[int, str]]:
    candidates = exact_protected_spans(
        [atom.atom_mapping() for atom in atoms], glossary_terms
    )
    chosen: dict[int, tuple[int, str]] = {}
    occupied: set[int] = set()
    for start, end, term in sorted(candidates, key=lambda item: (-(item[1] - item[0]), item[0])):
        if any(index in occupied for index in range(start, end)):
            continue
        chosen[start] = (end, term)
        occupied.update(range(start, end))
    return chosen


def _choose_cue_end(
    atoms: Sequence[AtomView],
    start: int,
    protected: Mapping[int, tuple[int, str]],
    config: SegmenterConfig,
) -> tuple[int, bool, str | None]:
    first_end, first_term = protected.get(start, (start + 1, None))
    if first_end - start > config.max_display_units:
        return first_end, True, first_term
    unit_ends: list[int] = []
    cursor = start
    while cursor < len(atoms):
        unit_end, _ = protected.get(cursor, (cursor + 1, None))
        if unit_end - start > config.max_display_units:
            break
        unit_ends.append(unit_end)
        cursor = unit_end
    if not unit_ends:
        raise ContractError("Segmenter could not make forward progress")
    terminal = [end for end in unit_ends if _is_terminal(atoms[end - 1].decoration_after)]
    if terminal:
        return terminal[0], False, None
    capacity_end = unit_ends[-1]
    if capacity_end == len(atoms):
        return capacity_end, False, None
    punctuation = [
        end for end in unit_ends if _is_punctuation_boundary(atoms[end - 1].decoration_after)
    ]
    if punctuation:
        return punctuation[-1], False, None
    clause_starts = [
        index
        for index in range(start + 1, capacity_end)
        if atoms[index].atom_class == "word"
        and atoms[index].text.casefold() in config.english_clause_starters
        and index in unit_ends
    ]
    return (clause_starts[-1] if clause_starts else capacity_end), False, None


def _is_terminal(decoration: str) -> bool:
    return any(character in "。；;？?！!." for character in decoration)


def _is_punctuation_boundary(decoration: str) -> bool:
    return any(character in "，,、：:" for character in decoration)


def _atom_spans(atoms: Sequence[AtomView]) -> list[dict[str, Any]]:
    spans: list[dict[str, Any]] = []
    for atom in atoms:
        if (
            spans
            and spans[-1]["transcript_artifact_id"] == atom.transcript_artifact_id
            and spans[-1]["end_position_exclusive"] == atom.position
        ):
            spans[-1]["end_position_exclusive"] = atom.position + 1
            spans[-1]["last_atom_id"] = atom.atom_id
        else:
            spans.append(
                {
                    "transcript_artifact_id": atom.transcript_artifact_id,
                    "chunk_id": atom.chunk_id,
                    "start_position": atom.position,
                    "end_position_exclusive": atom.position + 1,
                    "first_atom_id": atom.atom_id,
                    "last_atom_id": atom.atom_id,
                }
            )
    return spans
