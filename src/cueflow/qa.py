from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from cueflow.canonical import hash_json
from cueflow.config import QaRulesetConfig
from cueflow.schema import ArtifactEnvelope, validate_qa_payload


def structural_issues(
    transcript: ArtifactEnvelope,
    alignment: ArtifactEnvelope,
    subtitle: ArtifactEnvelope,
    *,
    duration_ms: int,
) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    if alignment.payload.get("transcript_artifact_id") != transcript.artifact_id:
        issues.append(_blocking("alignment_transcript_identity_mismatch"))
    if subtitle.payload.get("transcript_artifact_id") != transcript.artifact_id:
        issues.append(_blocking("subtitle_transcript_identity_mismatch"))
    if subtitle.payload.get("alignment_artifact_id") != alignment.artifact_id:
        issues.append(_blocking("subtitle_alignment_identity_mismatch"))
    assignments = alignment.payload.get("assignments")
    atoms = transcript.payload.get("atoms")
    if not isinstance(assignments, list) or not isinstance(atoms, list):
        issues.append(_blocking("missing_alignment_or_transcript_atoms"))
    elif [item.get("atom_id") for item in assignments] != [item.get("atom_id") for item in atoms]:
        issues.append(_blocking("alignment_atom_coverage_mismatch"))
    cues = subtitle.payload.get("cues")
    if not isinstance(cues, list) or not cues:
        issues.append(_blocking("subtitle_has_no_cues"))
    else:
        previous_end = -1
        for cue in cues:
            if not isinstance(cue, Mapping):
                issues.append(_blocking("invalid_subtitle_cue"))
                break
            start = cue.get("global_start_ms")
            end = cue.get("global_end_ms")
            if (
                not isinstance(start, int)
                or not isinstance(end, int)
                or start < previous_end
                or start < 0
                or end <= start
                or end > duration_ms
            ):
                issues.append(_blocking("subtitle_timeline_invalid"))
                break
            previous_end = end
    return issues


def qa_payload(
    subject_artifact_ids: Sequence[str], issues: Sequence[Mapping[str, Any]]
) -> dict[str, Any]:
    prepared: list[dict[str, Any]] = []
    for ordinal, issue in enumerate(issues):
        value = dict(issue)
        value.setdefault(
            "issue_id",
            "issue_" + hash_json({"ordinal": ordinal, "issue": value})[7:23],
        )
        prepared.append(value)
    if any(item.get("severity") == "blocking_error" for item in prepared):
        result = "blocked"
    elif prepared:
        result = "warnings"
    else:
        result = "passed"
    payload = {
        "subject_artifact_ids": list(subject_artifact_ids),
        "qa_ruleset_version": QaRulesetConfig().version,
        "result": result,
        "issues": prepared,
    }
    validate_qa_payload(payload)
    return payload


def _blocking(code: str) -> dict[str, Any]:
    return {
        "severity": "blocking_error",
        "code": code,
        "resolution_status": "unresolved",
    }
