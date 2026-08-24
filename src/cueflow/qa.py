from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from cueflow.atomizer import atom_signature, atomize, normalized_atom_text
from cueflow.config import QaRulesetConfig
from cueflow.errors import ContractError
from cueflow.glossary import glossary_atom_sequences
from cueflow.registry import Registry
from cueflow.schema import (
    ArtifactEnvelope,
    find_unaligned_atoms,
    validate_alignment_against,
    validate_subtitle_payload,
)


@dataclass(frozen=True)
class SemanticDecision:
    action: str
    issues: tuple[dict[str, Any], ...]
    rework_context: str | None = None


def glossary_single_atom_conflicts(
    transcript: Mapping[str, Any], terms: Sequence[str]
) -> list[dict[str, Any]]:
    atoms = transcript.get("atoms")
    if not isinstance(atoms, list):
        raise ContractError("Transcript atoms are missing")
    result: list[dict[str, Any]] = []
    for term_info in glossary_atom_sequences(terms):
        term_atoms = term_info["atoms"]
        if len(term_atoms) < 2:
            continue
        length = len(term_atoms)
        for start in range(0, len(atoms) - length + 1):
            window = atoms[start : start + length]
            classes = [str(atom["atom_class"]) for atom in window]
            if classes != term_info["classes"]:
                continue
            normalized = [normalized_atom_text(atom) for atom in window]
            differences = [
                index
                for index, (actual, expected) in enumerate(
                    zip(normalized, term_info["normalized"], strict=True)
                )
                if actual != expected
            ]
            if len(differences) == 1:
                result.append(
                    {
                        "term": str(term_info["term"]),
                        "start_position": start,
                        "end_position_exclusive": start + length,
                        "candidate_sequence": normalized,
                        "class_sequence": classes,
                        "differing_atom_offset": differences[0],
                        "atom_ids": [str(atom["atom_id"]) for atom in window],
                    }
                )
    return result


def evaluate_semantic_attempts(
    attempts: Sequence[Mapping[str, Any]],
    terms: Sequence[str],
    config: QaRulesetConfig | None = None,
) -> SemanticDecision:
    chosen = config or QaRulesetConfig()
    if not attempts:
        raise ContractError("semantic stability evaluation requires at least one attempt")
    if len(attempts) > chosen.semantic_attempt_limit:
        raise ContractError("semantic attempt limit exceeded")
    conflict_history = [glossary_single_atom_conflicts(attempt, terms) for attempt in attempts]
    latest_conflicts = conflict_history[-1]
    provider_history = [bool(attempt.get("provider_uncertain_spans")) for attempt in attempts]
    latest_provider_triggered = provider_history[-1]
    if len(attempts) == 1:
        if not latest_conflicts and not latest_provider_triggered:
            return SemanticDecision(action="accepted", issues=())
        return SemanticDecision(
            action="rework",
            issues=tuple(_rework_issues(latest_conflicts, latest_provider_triggered, 1)),
            rework_context=_rework_context(latest_conflicts, latest_provider_triggered),
        )

    previous = attempts[-2]
    latest = attempts[-1]
    previous_conflicts = conflict_history[-2]
    previous_signature = _attempt_candidate_signature(previous, previous_conflicts)
    latest_signature = _attempt_candidate_signature(latest, previous_conflicts)
    previous_conflict_keys = {
        (str(conflict["term"]), int(conflict["start_position"]))
        for conflict in previous_conflicts
    }
    latest_conflict_keys = {
        (str(conflict["term"]), int(conflict["start_position"]))
        for conflict in latest_conflicts
    }
    historical_conflicts = _unique_conflicts(
        conflict for conflicts in conflict_history for conflict in conflicts
    )
    historical_candidates_present = all(
        _candidate_for_conflict(latest, conflict) is not None
        for conflict in historical_conflicts
    )
    stable = (
        previous_signature == latest_signature
        and _all_candidates_present(latest_signature)
        and historical_candidates_present
        and latest_conflict_keys.issubset(previous_conflict_keys)
        and provider_history[-2] == latest_provider_triggered
    )
    if stable:
        issues: list[dict[str, Any]] = []
        latest_keys = {
            (str(conflict["term"]), int(conflict["start_position"]))
            for conflict in latest_conflicts
        }
        for conflict in latest_conflicts:
            issues.append(
                {
                    "severity": "warning",
                    "code": "stable_glossary_conflict",
                    "resolution_status": "unresolved",
                    "semantic_attempts": len(attempts),
                    "locations": [
                        {
                            "chunk_id": latest.get("chunk_id"),
                            "atom_span": [
                                conflict["start_position"],
                                conflict["end_position_exclusive"],
                            ],
                        }
                    ],
                    "observed": conflict,
                }
            )
        for historical_conflict in historical_conflicts:
            key = (
                str(historical_conflict["term"]),
                int(historical_conflict["start_position"]),
            )
            if key in latest_keys:
                continue
            candidate = _candidate_for_conflict(latest, historical_conflict)
            assert candidate is not None
            issues.append(
                {
                    "severity": "warning",
                    "code": "glossary_single_atom_conflict",
                    "resolution_status": "resolved",
                    "semantic_attempts": len(attempts),
                    "locations": [{"chunk_id": latest.get("chunk_id")}],
                    "observed": {
                        "term": historical_conflict["term"],
                        "stable_match": True,
                        "candidate_sequence": candidate["candidate_sequence"],
                    },
                }
            )
        if latest_provider_triggered:
            issues.append(
                {
                    "severity": "warning",
                    "code": "provider_marked_uncertain",
                    "resolution_status": "unresolved",
                    "semantic_attempts": len(attempts),
                    "locations": [{"chunk_id": latest.get("chunk_id")}],
                    "observed": {
                        "spans": latest.get("provider_uncertain_spans"),
                        "stable_transcription": True,
                    },
                }
            )
        return SemanticDecision(action="accepted", issues=tuple(issues))

    if len(attempts) >= chosen.semantic_attempt_limit:
        triggered_terms = sorted(
            {str(item["term"]) for item in historical_conflicts}
        )
        code = "unstable_glossary_conflict" if triggered_terms else "provider_marked_uncertain"
        issue = {
            "severity": "warning",
            "code": code,
            "resolution_status": "unresolved",
            "semantic_attempts": len(attempts),
            "locations": [{"chunk_id": latest.get("chunk_id")}],
            "observed": {
                "terms": triggered_terms,
                "current_conflicts": [dict(item) for item in latest_conflicts],
                "consecutive_candidate_sequences_equal": False,
            },
        }
        return SemanticDecision(action="accepted", issues=(issue,))
    rework_conflicts = latest_conflicts or historical_conflicts
    return SemanticDecision(
        action="rework",
        issues=tuple(
            _rework_issues(latest_conflicts, latest_provider_triggered, len(attempts))
        ),
        rework_context=_rework_context(
            rework_conflicts, latest_provider_triggered or provider_history[-2]
        ),
    )


def structural_issues(
    media_chunks: Sequence[ArtifactEnvelope],
    transcripts: Sequence[ArtifactEnvelope],
    alignments: Sequence[ArtifactEnvelope],
    subtitle: ArtifactEnvelope,
    *,
    duration_ms: int,
    registry: Registry | None = None,
    project_id: str | None = None,
) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    chunks = {str(item.payload.get("chunk_id")): item for item in media_chunks}
    transcript_by_chunk = {str(item.payload.get("chunk_id")): item for item in transcripts}
    alignment_by_chunk = {str(item.payload.get("chunk_id")): item for item in alignments}
    if set(chunks) != set(transcript_by_chunk) or set(chunks) != set(alignment_by_chunk):
        issues.append(_blocking("chunk_reference_mismatch", {"chunks": sorted(chunks)}))
    for chunk_id, chunk in chunks.items():
        transcript = transcript_by_chunk.get(chunk_id)
        alignment = alignment_by_chunk.get(chunk_id)
        if transcript is None or alignment is None:
            continue
        try:
            if alignment.payload.get("transcript_artifact_id") != transcript.artifact_id:
                raise ContractError("Alignment references a different Transcript Artifact")
            if alignment.payload.get("media_chunk_artifact_id") != chunk.artifact_id:
                raise ContractError("Alignment references a different MediaChunk Artifact")
            validate_alignment_against(alignment.payload, transcript.payload, chunk.payload)
            unaligned = find_unaligned_atoms(alignment.payload)
            if unaligned:
                raise ContractError(f"unaligned real-sound atoms: {unaligned}")
            _require_envelope_input(alignment, "transcript", transcript.artifact_id)
            _require_envelope_input(alignment, "media_chunk", chunk.artifact_id)
        except ContractError as exc:
            issues.append(
                _blocking(
                    "alignment_structural_error",
                    {"chunk_id": chunk_id, "detail": str(exc)},
                )
            )
    try:
        validate_subtitle_payload(subtitle.payload)
        previous_end = -1
        for cue in subtitle.payload.get("cues", []):
            start = int(cue["global_start_ms"])
            end = int(cue["global_end_ms"])
            if start < previous_end or end > duration_ms:
                raise ContractError("Cue overlaps, is unordered, or exceeds timeline")
            previous_end = end
        expected = {item.artifact_id for item in [*transcripts, *alignments]}
        actual = {
            item.artifact_id
            for item in subtitle.inputs
            if item.role in {"transcript", "alignment"} and item.artifact_id is not None
        }
        if expected != actual:
            raise ContractError("Subtitle dependencies do not match current Chunk Artifacts")
    except (ContractError, KeyError, TypeError, ValueError) as exc:
        issues.append(_blocking("subtitle_structural_error", {"detail": str(exc)}))
    if registry is not None and project_id is not None:
        for envelope in [*media_chunks, *transcripts, *alignments, subtitle]:
            try:
                _validate_registry_dependencies(registry, project_id, envelope)
            except ContractError as exc:
                issues.append(
                    _blocking(
                        "artifact_dependency_identity_mismatch",
                        {"artifact_id": envelope.artifact_id, "detail": str(exc)},
                    )
                )
    return issues


def alignment_repair_workset(
    issues: Sequence[Mapping[str, Any]],
) -> tuple[str, ...]:
    """Return the distinct chunks eligible for one QA alignment repair wave."""
    chunk_ids: set[str] = set()
    for issue in issues:
        if issue.get("severity") != "blocking_error":
            continue
        if issue.get("code") != "alignment_structural_error":
            continue
        observed = issue.get("observed")
        if not isinstance(observed, Mapping):
            continue
        chunk_id = observed.get("chunk_id")
        if isinstance(chunk_id, str) and chunk_id:
            chunk_ids.add(chunk_id)
    return tuple(sorted(chunk_ids))


def possible_chunk_boundary_duplication(
    transcripts: Sequence[ArtifactEnvelope], max_window_atoms: int = 8
) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    for left, right in zip(transcripts, transcripts[1:], strict=False):
        left_atoms = list(left.payload.get("atoms", []))
        right_atoms = list(right.payload.get("atoms", []))
        limit = min(max_window_atoms, len(left_atoms), len(right_atoms))
        overlap = 0
        for size in range(2, limit + 1):
            left_signature = [normalized_atom_text(atom) for atom in left_atoms[-size:]]
            right_signature = [normalized_atom_text(atom) for atom in right_atoms[:size]]
            if left_signature == right_signature:
                overlap = size
        if overlap:
            issues.append(
                {
                    "severity": "warning",
                    "code": "possible_chunk_boundary_duplication",
                    "resolution_status": "unresolved",
                    "locations": [
                        {
                            "artifact_id": left.artifact_id,
                            "atom_span": [len(left_atoms) - overlap, len(left_atoms)],
                        },
                        {"artifact_id": right.artifact_id, "atom_span": [0, overlap]},
                    ],
                    "observed": {
                        "normalized_overlap": [
                            normalized_atom_text(atom) for atom in right_atoms[:overlap]
                        ]
                    },
                }
            )
    return issues


def qa_payload(
    subject_artifact_ids: Sequence[str], issues: Sequence[Mapping[str, Any]]
) -> dict[str, Any]:
    copied = []
    for index, item in enumerate(issues, start=1):
        issue = dict(item)
        issue["issue_id"] = f"issue_{index:05d}"
        copied.append(issue)
    blocked = any(item.get("severity") == "blocking_error" for item in copied)
    result = "blocked" if blocked else ("warnings" if copied else "passed")
    return {
        "subject_artifact_ids": list(subject_artifact_ids),
        "qa_ruleset_version": QaRulesetConfig().version,
        "result": result,
        "issues": copied,
    }


def _unique_conflicts(
    conflicts: Iterable[Mapping[str, Any]],
) -> list[Mapping[str, Any]]:
    unique: list[Mapping[str, Any]] = []
    seen: set[tuple[str, int]] = set()
    for conflict in conflicts:
        key = (str(conflict["term"]), int(conflict["start_position"]))
        if key not in seen:
            seen.add(key)
            unique.append(conflict)
    return unique


def _attempt_candidate_signature(
    transcript: Mapping[str, Any], initial_conflicts: Sequence[Mapping[str, Any]]
) -> tuple[Any, ...]:
    atoms = transcript.get("atoms")
    if not isinstance(atoms, list):
        raise ContractError("Transcript atoms are missing")
    if not initial_conflicts:
        return atom_signature(atoms)
    return tuple(
        (
            str(conflict["term"]),
            int(conflict["start_position"]),
            (
                tuple(candidate["candidate_sequence"])
                if (candidate := _candidate_for_conflict(transcript, conflict)) is not None
                else None
            ),
        )
        for conflict in initial_conflicts
    )


def _candidate_for_conflict(
    transcript: Mapping[str, Any], initial_conflict: Mapping[str, Any]
) -> dict[str, Any] | None:
    atoms = transcript.get("atoms")
    if not isinstance(atoms, list):
        raise ContractError("Transcript atoms are missing")
    _, term_atoms = atomize(str(initial_conflict["term"]))
    length = len(term_atoms)
    term_classes = [str(atom["atom_class"]) for atom in term_atoms]
    term_normalized = [normalized_atom_text(atom) for atom in term_atoms]
    initial_start = int(initial_conflict["start_position"])
    choices: list[tuple[int, int, int, tuple[str, ...]]] = []
    for start in range(0, len(atoms) - length + 1):
        window = atoms[start : start + length]
        if [str(atom["atom_class"]) for atom in window] != term_classes:
            continue
        normalized = tuple(normalized_atom_text(atom) for atom in window)
        difference_count = sum(
            actual != expected
            for actual, expected in zip(normalized, term_normalized, strict=True)
        )
        if difference_count <= 1:
            choices.append((abs(start - initial_start), difference_count, start, normalized))
    if not choices:
        return None
    _, difference_count, start, normalized = min(choices)
    return {
        "start_position": start,
        "end_position_exclusive": start + length,
        "candidate_sequence": normalized,
        "difference_count": difference_count,
    }


def _all_candidates_present(signature: tuple[Any, ...]) -> bool:
    if not signature:
        return True
    first = signature[0]
    if not isinstance(first, tuple) or len(first) != 3:
        return True
    return all(item[2] is not None for item in signature)


def _rework_issues(
    conflicts: Sequence[Mapping[str, Any]], provider_triggered: bool, attempt_count: int
) -> list[dict[str, Any]]:
    issues = [
        {
            "severity": "warning",
            "code": "glossary_single_atom_conflict",
            "resolution_status": "rework_requested",
            "semantic_attempts": attempt_count,
            "locations": [
                {
                    "atom_span": [
                        conflict["start_position"],
                        conflict["end_position_exclusive"],
                    ]
                }
            ],
            "observed": dict(conflict),
        }
        for conflict in conflicts
    ]
    if provider_triggered:
        issues.append(
            {
                "severity": "warning",
                "code": "provider_marked_uncertain",
                "resolution_status": "rework_requested",
                "semantic_attempts": attempt_count,
                "locations": [],
                "observed": {},
            }
        )
    return issues


def _rework_context(conflicts: Sequence[Mapping[str, Any]], provider_triggered: bool) -> str:
    terms = sorted({str(item["term"]) for item in conflicts})
    parts = []
    if terms:
        parts.append(
            "请重新核对疑似专名/术语附近的实际发音："
            + "、".join(terms)
            + "。词库只是线索，不得强制替换。"
        )
    if provider_triggered:
        parts.append("请重新核对 Provider 上轮明确标记为不确定的跨度。")
    return "\n".join(parts)


def _blocking(code: str, observed: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "severity": "blocking_error",
        "code": code,
        "resolution_status": "unresolved",
        "locations": [],
        "observed": dict(observed),
    }


def _require_envelope_input(envelope: ArtifactEnvelope, role: str, artifact_id: str) -> None:
    if not any(item.role == role and item.artifact_id == artifact_id for item in envelope.inputs):
        raise ContractError(f"{envelope.artifact_kind} is missing exact {role} dependency")


def _validate_registry_dependencies(
    registry: Registry, project_id: str, envelope: ArtifactEnvelope
) -> None:
    rows = registry.dependencies(project_id, envelope.artifact_id)
    actual = [
        (
            str(row["role"]),
            str(row["input_artifact_id"]) if row["input_artifact_id"] else None,
            str(row["input_source_asset_id"]) if row["input_source_asset_id"] else None,
        )
        for row in rows
    ]
    expected = [(item.role, item.artifact_id, item.source_asset_id) for item in envelope.inputs]
    if actual != expected:
        raise ContractError("Registry dependency edges differ from the Artifact envelope")
