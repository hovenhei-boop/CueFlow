from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, cast

from cueflow.canonical import artifact_content_hash
from cueflow.config import SCHEMA_VERSION
from cueflow.errors import ContractError

ARTIFACT_KINDS = frozenset(
    {
        "media_probe",
        "timeline_audio",
        "video_proxy",
        "chunk_plan",
        "media_chunk",
        "system_glossary",
        "project_glossary",
        "effective_glossary",
        "transcript",
        "alignment",
        "subtitle",
        "qa",
        "filler_review",
        "srt_render",
    }
)
CHUNK_KINDS = frozenset({"media_chunk", "transcript", "alignment"})
GLOSSARY_KINDS = frozenset({"system_glossary", "project_glossary", "effective_glossary"})
ATOM_CLASSES = frozenset({"word", "cjk_character", "number", "pronounceable_symbol"})
INT64_MAX = 2**63 - 1


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True)
class Producer:
    component: str
    component_version: str
    processing_profile: str | None
    provider: str | None
    model: str | None
    config_hash: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "component": self.component,
            "component_version": self.component_version,
            "processing_profile": self.processing_profile,
            "provider": self.provider,
            "model": self.model,
            "config_hash": self.config_hash,
        }


@dataclass(frozen=True)
class InputRef:
    role: str
    artifact_id: str | None = None
    source_asset_id: str | None = None
    coordinate_range: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        if bool(self.artifact_id) == bool(self.source_asset_id):
            raise ContractError("InputRef requires exactly one artifact_id or source_asset_id")

    def as_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {"role": self.role}
        if self.artifact_id is not None:
            result["artifact_id"] = self.artifact_id
        if self.source_asset_id is not None:
            result["source_asset_id"] = self.source_asset_id
        if self.coordinate_range is not None:
            result["coordinate_range"] = dict(self.coordinate_range)
        return result


@dataclass(frozen=True)
class ArtifactEnvelope:
    schema_version: str
    artifact_id: str
    artifact_kind: str
    scope_key: str
    content_hash: str
    created_at: str
    producer: Producer
    inputs: tuple[InputRef, ...]
    payload: Mapping[str, Any]

    @classmethod
    def create(
        cls,
        *,
        artifact_kind: str,
        scope_key: str,
        producer: Producer,
        inputs: Sequence[InputRef],
        payload: Mapping[str, Any],
        created_at: str | None = None,
    ) -> ArtifactEnvelope:
        validate_scope(artifact_kind, scope_key, payload)
        validate_payload(artifact_kind, payload)
        input_dicts = [item.as_dict() for item in inputs]
        content_hash = artifact_content_hash(
            artifact_kind=artifact_kind,
            scope_key=scope_key,
            schema_version=SCHEMA_VERSION,
            producer=producer.as_dict(),
            inputs=input_dicts,
            payload=payload,
        )
        envelope = cls(
            schema_version=SCHEMA_VERSION,
            artifact_id="art_" + content_hash.removeprefix("sha256:"),
            artifact_kind=artifact_kind,
            scope_key=scope_key,
            content_hash=content_hash,
            created_at=created_at or utc_now(),
            producer=producer,
            inputs=tuple(inputs),
            payload=dict(payload),
        )
        envelope.validate()
        return envelope

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> ArtifactEnvelope:
        producer_value = _mapping(value.get("producer"), "producer")
        producer = Producer(
            component=_string(producer_value.get("component"), "producer.component"),
            component_version=_string(
                producer_value.get("component_version"), "producer.component_version"
            ),
            processing_profile=_optional_string(producer_value.get("processing_profile")),
            provider=_optional_string(producer_value.get("provider")),
            model=_optional_string(producer_value.get("model")),
            config_hash=_string(producer_value.get("config_hash"), "producer.config_hash"),
        )
        inputs_value = value.get("inputs")
        if not isinstance(inputs_value, list):
            raise ContractError("inputs must be an array")
        inputs = tuple(
            InputRef(
                role=_string(item.get("role"), "inputs.role"),
                artifact_id=_optional_string(item.get("artifact_id")),
                source_asset_id=_optional_string(item.get("source_asset_id")),
                coordinate_range=item.get("coordinate_range"),
            )
            for raw in inputs_value
            for item in [_mapping(raw, "inputs[]")]
        )
        envelope = cls(
            schema_version=_string(value.get("schema_version"), "schema_version"),
            artifact_id=_string(value.get("artifact_id"), "artifact_id"),
            artifact_kind=_string(value.get("artifact_kind"), "artifact_kind"),
            scope_key=_string(value.get("scope_key"), "scope_key"),
            content_hash=_string(value.get("content_hash"), "content_hash"),
            created_at=_string(value.get("created_at"), "created_at"),
            producer=producer,
            inputs=inputs,
            payload=_mapping(value.get("payload"), "payload"),
        )
        envelope.validate()
        return envelope

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "artifact_id": self.artifact_id,
            "artifact_kind": self.artifact_kind,
            "scope_key": self.scope_key,
            "content_hash": self.content_hash,
            "created_at": self.created_at,
            "producer": self.producer.as_dict(),
            "inputs": [item.as_dict() for item in self.inputs],
            "payload": dict(self.payload),
        }

    def validate(self) -> None:
        major = _schema_major(self.schema_version)
        if major != 1:
            raise ContractError(f"unsupported schema major version: {major}")
        validate_scope(self.artifact_kind, self.scope_key, self.payload)
        validate_payload(self.artifact_kind, self.payload)
        expected_hash = artifact_content_hash(
            artifact_kind=self.artifact_kind,
            scope_key=self.scope_key,
            schema_version=self.schema_version,
            producer=self.producer.as_dict(),
            inputs=[item.as_dict() for item in self.inputs],
            payload=self.payload,
        )
        if expected_hash != self.content_hash:
            raise ContractError("artifact content_hash does not match semantic content")
        if self.artifact_id != "art_" + self.content_hash.removeprefix("sha256:"):
            raise ContractError("artifact_id does not match content_hash")


def validate_scope(kind: str, scope_key: str, payload: Mapping[str, Any]) -> None:
    if kind not in ARTIFACT_KINDS:
        raise ContractError(f"unknown artifact kind: {kind}")
    if kind in CHUNK_KINDS:
        chunk_id = payload.get("chunk_id")
        if not isinstance(chunk_id, str) or chunk_id != scope_key:
            raise ContractError(f"{kind} scope_key must equal payload.chunk_id")
    elif scope_key != "global":
        raise ContractError(f"{kind} must use global scope_key")


def validate_payload(kind: str, payload: Mapping[str, Any]) -> None:
    if kind in GLOSSARY_KINDS:
        validate_glossary_payload(payload)
    elif kind == "transcript":
        validate_transcript_payload(payload)
    elif kind == "alignment":
        validate_alignment_payload(payload)
    elif kind == "subtitle":
        validate_subtitle_payload(payload)
    elif kind == "qa":
        validate_qa_payload(payload)
    elif kind == "filler_review":
        validate_filler_review_payload(payload)
    elif kind == "media_chunk":
        validate_interval(payload, "global_start_ms", "global_end_ms")
        _string(payload.get("timeline_audio_artifact_id"), "timeline_audio_artifact_id")
    elif kind == "chunk_plan":
        chunks = payload.get("chunks")
        if not isinstance(chunks, list) or not chunks:
            raise ContractError("chunk_plan.chunks must be a non-empty array")
        _string(payload.get("timeline_audio_artifact_id"), "timeline_audio_artifact_id")
        _validate_chunk_coverage(chunks, _integer(payload.get("duration_ms"), "duration_ms"))
    elif kind == "timeline_audio":
        if _integer(payload.get("duration_ms"), "duration_ms") <= 0:
            raise ContractError("timeline_audio duration_ms must be positive")
        if (
            payload.get("sample_rate_hz") != 16_000
            or payload.get("channels") != 1
            or payload.get("sample_format") != "s16le"
        ):
            raise ContractError("timeline_audio must be 16kHz mono PCM s16le")
    elif kind == "video_proxy":
        if payload.get("authoritative_for_audio_processing") is not False:
            raise ContractError("video_proxy cannot be authoritative for audio processing")
        if _integer(payload.get("max_width"), "max_width") > 640:
            raise ContractError("video_proxy max_width exceeds 640")
        if _integer(payload.get("max_height"), "max_height") > 360:
            raise ContractError("video_proxy max_height exceeds 360")
        width = _integer(payload.get("width"), "width")
        height = _integer(payload.get("height"), "height")
        if width <= 0 or height <= 0 or width > 640 or height > 360:
            raise ContractError("video_proxy dimensions exceed the frozen boundary")
    elif kind == "media_probe":
        if payload.get("timeline_status") not in {"normal", "corrected", "unverified"}:
            raise ContractError("invalid media_probe timeline_status")
        if _integer(payload.get("presentation_duration_ms"), "presentation_duration_ms") <= 0:
            raise ContractError("media_probe presentation duration must be positive")
        if payload.get("timeline_tolerance_ms") != 20:
            raise ContractError("media_probe timeline tolerance must be 20ms")
    elif kind == "srt_render":
        if not isinstance(payload.get("text"), str):
            raise ContractError("srt_render.text must be a string")


def validate_glossary_payload(payload: Mapping[str, Any]) -> None:
    terms = payload.get("terms")
    if not isinstance(terms, list) or any(not isinstance(term, str) or not term for term in terms):
        raise ContractError("glossary terms must be non-empty strings")
    if len(terms) != len(set(terms)):
        raise ContractError("glossary terms must be unique")
    if terms != sorted(terms):
        raise ContractError("glossary terms must be deterministically sorted")
    if payload.get("normalization_version") != "0.1.0":
        raise ContractError("unsupported glossary normalization version")


def validate_transcript_payload(payload: Mapping[str, Any]) -> None:
    source_text = _string(payload.get("source_text"), "source_text", allow_empty=True)
    leading = _string(payload.get("leading_decoration", ""), "leading_decoration", allow_empty=True)
    atoms = payload.get("atoms")
    if not isinstance(atoms, list):
        raise ContractError("transcript.atoms must be an array")
    if payload.get("atomizer_version") != "0.1.0":
        raise ContractError("unsupported atomizer version")
    rebuilt = leading
    seen: set[str] = set()
    for position, raw in enumerate(atoms):
        atom = _mapping(raw, "atoms[]")
        atom_id = _string(atom.get("atom_id"), "atom_id")
        if atom_id in seen:
            raise ContractError("duplicate atom_id")
        seen.add(atom_id)
        if _integer(atom.get("position"), "position") != position:
            raise ContractError("atom positions must be contiguous")
        if atom.get("atom_class") not in ATOM_CLASSES:
            raise ContractError("invalid atom_class")
        text = _string(atom.get("text"), "atom.text")
        decoration = _string(
            atom.get("decoration_after", ""), "atom.decoration_after", allow_empty=True
        )
        rebuilt += text + decoration
    if rebuilt != source_text:
        raise ContractError("transcript atoms and decorations do not rebuild source_text")


def validate_alignment_payload(payload: Mapping[str, Any]) -> None:
    _string(payload.get("transcript_artifact_id"), "transcript_artifact_id")
    _string(payload.get("media_chunk_artifact_id"), "media_chunk_artifact_id")
    assignments = payload.get("assignments")
    if not isinstance(assignments, list):
        raise ContractError("alignment.assignments must be an array")
    seen: set[str] = set()
    previous_start = -1
    for raw in assignments:
        item = _mapping(raw, "assignments[]")
        atom_id = _string(item.get("atom_id"), "assignment.atom_id")
        if atom_id in seen:
            raise ContractError("duplicate alignment atom_id")
        seen.add(atom_id)
        status = item.get("status")
        if status == "aligned":
            validate_interval(item, "global_start_ms", "global_end_ms")
            start = _integer(item.get("global_start_ms"), "global_start_ms")
            if start < previous_start:
                raise ContractError("alignment assignments are not ordered")
            previous_start = start
            confidence = item.get("acoustic_confidence")
            if confidence is not None and not isinstance(confidence, Mapping):
                raise ContractError("acoustic_confidence must preserve provider metadata")
        elif status == "unaligned":
            _string(item.get("reason"), "unaligned.reason")
        else:
            raise ContractError("invalid alignment assignment status")


def validate_subtitle_payload(payload: Mapping[str, Any]) -> None:
    _string(payload.get("segmenter_config_hash"), "segmenter_config_hash")
    for name in ("transcript_artifact_ids", "alignment_artifact_ids"):
        values = payload.get(name)
        if not isinstance(values, list) or any(not isinstance(item, str) for item in values):
            raise ContractError(f"subtitle.{name} must be an array of Artifact IDs")
    cues = payload.get("cues")
    if not isinstance(cues, list):
        raise ContractError("subtitle.cues must be an array")
    previous_end = -1
    cue_ids: set[str] = set()
    for raw in cues:
        cue = _mapping(raw, "cues[]")
        cue_id = _string(cue.get("cue_id"), "cue_id")
        if cue_id in cue_ids:
            raise ContractError("duplicate cue_id")
        cue_ids.add(cue_id)
        validate_interval(cue, "global_start_ms", "global_end_ms")
        start = _integer(cue.get("global_start_ms"), "global_start_ms")
        end = _integer(cue.get("global_end_ms"), "global_end_ms")
        if start < previous_end:
            raise ContractError("subtitle cues overlap or are unordered")
        previous_end = end
        _string(cue.get("text"), "cue.text")
        count = _integer(cue.get("display_unit_count"), "display_unit_count")
        overflow = cue.get("protected_overflow")
        if not isinstance(overflow, bool):
            raise ContractError("protected_overflow must be boolean")
        if count > 10 and not overflow:
            raise ContractError("cue exceeds 10 display units without protected overflow")
        if not isinstance(cue.get("atom_spans"), list) or not cue["atom_spans"]:
            raise ContractError("cue requires atom_spans")
        atom_refs = cue.get("atom_refs")
        if not isinstance(atom_refs, list) or len(atom_refs) != count:
            raise ContractError("cue atom_refs must exactly cover its display units")
        for raw_span in cue["atom_spans"]:
            span = _mapping(raw_span, "atom_spans[]")
            _string(span.get("transcript_artifact_id"), "span.transcript_artifact_id")
            _string(span.get("first_atom_id"), "span.first_atom_id")
            _string(span.get("last_atom_id"), "span.last_atom_id")


def validate_qa_payload(payload: Mapping[str, Any]) -> None:
    subjects = payload.get("subject_artifact_ids")
    if not isinstance(subjects, list) or any(not isinstance(item, str) for item in subjects):
        raise ContractError("qa.subject_artifact_ids must be an array of Artifact IDs")
    if payload.get("qa_ruleset_version") != "0.1.0":
        raise ContractError("unsupported QA ruleset version")
    if payload.get("result") not in {"passed", "warnings", "blocked"}:
        raise ContractError("invalid QA result")
    issues = payload.get("issues")
    if not isinstance(issues, list):
        raise ContractError("qa.issues must be an array")
    issue_ids: set[str] = set()
    for raw in issues:
        issue = _mapping(raw, "issues[]")
        issue_id = _string(issue.get("issue_id"), "issue.issue_id")
        if issue_id in issue_ids:
            raise ContractError("duplicate QA issue_id")
        issue_ids.add(issue_id)
        if issue.get("severity") not in {"blocking_error", "warning"}:
            raise ContractError("invalid QA issue severity")
        if issue.get("resolution_status") not in {
            "detected",
            "rework_requested",
            "resolved",
            "unresolved",
        }:
            raise ContractError("invalid QA resolution_status")


def validate_filler_review_payload(payload: Mapping[str, Any]) -> None:
    if payload.get("mode") not in {"deterministic_local", "cloud_atom_review"}:
        raise ContractError("invalid filler review mode")
    if payload.get("status") not in {"completed", "unavailable"}:
        raise ContractError("invalid filler review status")
    candidates = payload.get("candidates")
    suppressions = payload.get("suppressions")
    if not isinstance(candidates, list) or not isinstance(suppressions, list):
        raise ContractError("filler candidates and suppressions must be arrays")
    candidate_text: dict[tuple[str, str, str], str] = {}
    cue_counts: dict[str, int] = {}
    for raw in candidates:
        candidate = _mapping(raw, "candidates[]")
        text = _string(candidate.get("text"), "candidate.text")
        if text not in {"啊", "呀", "哦", "嗯", "呃"}:
            raise ContractError("filler candidate is outside the frozen whitelist")
        key = (
            _string(candidate.get("cue_id"), "candidate.cue_id"),
            _string(candidate.get("transcript_artifact_id"), "candidate.transcript_artifact_id"),
            _string(candidate.get("atom_id"), "candidate.atom_id"),
        )
        if key in candidate_text:
            raise ContractError("duplicate filler candidate")
        candidate_text[key] = text
    for raw in suppressions:
        suppression = _mapping(raw, "suppressions[]")
        key = (
            _string(suppression.get("cue_id"), "suppression.cue_id"),
            _string(
                suppression.get("transcript_artifact_id"),
                "suppression.transcript_artifact_id",
            ),
            _string(suppression.get("atom_id"), "suppression.atom_id"),
        )
        if key not in candidate_text:
            raise ContractError("filler suppression is not a candidate")
        if suppression.get("text") != candidate_text[key]:
            raise ContractError("filler suppression text differs from its candidate")
        if suppression.get("reason") != "terminal_filler":
            raise ContractError("invalid filler suppression reason")
        cue_counts[key[0]] = cue_counts.get(key[0], 0) + 1
        if cue_counts[key[0]] > 1:
            raise ContractError("more than one filler suppression for a cue")
    if payload.get("status") == "unavailable" and suppressions:
        raise ContractError("unavailable filler review must not suppress atoms")


def validate_interval(value: Mapping[str, Any], start_name: str, end_name: str) -> None:
    start = _integer(value.get(start_name), start_name)
    end = _integer(value.get(end_name), end_name)
    if start < 0 or end > INT64_MAX or start >= end:
        raise ContractError(f"invalid half-open interval [{start}, {end})")


def atom_ids(payload: Mapping[str, Any]) -> list[str]:
    atoms = payload.get("atoms")
    if not isinstance(atoms, list):
        raise ContractError("transcript atoms missing")
    return [_string(_mapping(atom, "atoms[]").get("atom_id"), "atom_id") for atom in atoms]


def validate_alignment_against(
    alignment: Mapping[str, Any],
    transcript: Mapping[str, Any],
    media_chunk: Mapping[str, Any],
) -> None:
    if alignment.get("chunk_id") != transcript.get("chunk_id"):
        raise ContractError("alignment and transcript chunk_id mismatch")
    if alignment.get("chunk_id") != media_chunk.get("chunk_id"):
        raise ContractError("alignment and media chunk_id mismatch")
    expected = atom_ids(transcript)
    assignments = alignment.get("assignments")
    if not isinstance(assignments, list):
        raise ContractError("alignment assignments missing")
    actual = [_mapping(item, "assignments[]").get("atom_id") for item in assignments]
    if actual != expected:
        raise ContractError("alignment assignments do not exactly cover transcript atoms")
    chunk_start = _integer(media_chunk.get("global_start_ms"), "global_start_ms")
    chunk_end = _integer(media_chunk.get("global_end_ms"), "global_end_ms")
    for raw in assignments:
        item = _mapping(raw, "assignments[]")
        if item.get("status") == "aligned":
            if item["global_start_ms"] < chunk_start or item["global_end_ms"] > chunk_end:
                raise ContractError("alignment assignment falls outside media chunk")


def find_unaligned_atoms(alignment: Mapping[str, Any]) -> list[str]:
    assignments = alignment.get("assignments", [])
    return [
        _string(_mapping(item, "assignments[]").get("atom_id"), "atom_id")
        for item in assignments
        if _mapping(item, "assignments[]").get("status") == "unaligned"
    ]


def _validate_chunk_coverage(chunks: Iterable[Any], duration_ms: int) -> None:
    expected_start = 0
    for raw in chunks:
        chunk = _mapping(raw, "chunks[]")
        validate_interval(chunk, "global_start_ms", "global_end_ms")
        if chunk["global_start_ms"] != expected_start:
            raise ContractError("chunks must cover timeline without gaps or overlap")
        if chunk["global_end_ms"] - chunk["global_start_ms"] > 225_000:
            raise ContractError("chunk exceeds frozen 225 second hard limit")
        expected_start = chunk["global_end_ms"]
    if expected_start != duration_ms:
        raise ContractError("chunks do not cover the entire timeline")


def _schema_major(version: str) -> int:
    parts = version.split(".")
    if len(parts) != 3 or not all(part.isdigit() for part in parts):
        raise ContractError("invalid schema version")
    return int(parts[0])


def _mapping(value: Any, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ContractError(f"{name} must be an object")
    return cast(Mapping[str, Any], value)


def _integer(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ContractError(f"{name} must be an integer")
    return cast(int, value)


def _string(value: Any, name: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or (not allow_empty and not value):
        raise ContractError(f"{name} must be a string")
    return value


def _optional_string(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ContractError("optional string field has invalid type")
    return value
