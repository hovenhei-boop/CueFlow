from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlsplit

from cueflow.canonical import artifact_content_hash
from cueflow.config import ATOMIZER_VERSION, MAX_USER_KEYWORDS, SCHEMA_VERSION
from cueflow.errors import ContractError

ARTIFACT_KINDS = frozenset(
    {
        "job_input",
        "media_probe",
        "timeline_audio",
        "media_object",
        "base_asr",
        "peer_asr",
        "asr_comparison",
        "acoustic_window_plan",
        "acoustic_window",
        "glm_adjudication_evidence",
        "agreement_resolution",
        "acoustic_resolution",
        "review_resolution",
        "qwen_edit_proposal",
        "kimi_edit_proposal",
        "edit_proposal",
        "edit_resolution",
        "review_queue",
        "transcript",
        "alignment",
        "subtitle",
        "qa",
        "srt_render",
    }
)
SCOPED_KINDS = frozenset({"acoustic_window", "glm_adjudication_evidence", "acoustic_resolution"})
ATOM_CLASSES = frozenset({"word", "cjk_character", "number", "pronounceable_symbol"})
TEXT_REFERENCE_FORMATS = frozenset({"txt", "md", "csv", "json"})
URL_REFERENCE_KINDS = frozenset({"pdf_url", "image_url"})


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True)
class Producer:
    component: str
    component_version: str
    provider: str | None
    model: str | None
    config_hash: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "component": self.component,
            "component_version": self.component_version,
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
        if (self.artifact_id is None) == (self.source_asset_id is None):
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
        content_hash = artifact_content_hash(
            artifact_kind=artifact_kind,
            scope_key=scope_key,
            schema_version=SCHEMA_VERSION,
            producer=producer.as_dict(),
            inputs=[item.as_dict() for item in inputs],
            payload=payload,
        )
        envelope = cls(
            SCHEMA_VERSION,
            "art_" + content_hash.removeprefix("sha256:"),
            artifact_kind,
            scope_key,
            content_hash,
            created_at or utc_now(),
            producer,
            tuple(inputs),
            dict(payload),
        )
        envelope.validate()
        return envelope

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> ArtifactEnvelope:
        if set(value) != {
            "schema_version",
            "artifact_id",
            "artifact_kind",
            "scope_key",
            "content_hash",
            "created_at",
            "producer",
            "inputs",
            "payload",
        }:
            raise ContractError("ArtifactEnvelope fields do not match the current schema")
        raw_producer = _mapping(value["producer"], "producer")
        if set(raw_producer) != {
            "component",
            "component_version",
            "provider",
            "model",
            "config_hash",
        }:
            raise ContractError("producer fields do not match the current schema")
        producer = Producer(
            _string(raw_producer["component"], "producer.component"),
            _string(raw_producer["component_version"], "producer.component_version"),
            _optional_string(raw_producer["provider"], "producer.provider"),
            _optional_string(raw_producer["model"], "producer.model"),
            _string(raw_producer["config_hash"], "producer.config_hash"),
        )
        raw_inputs = value["inputs"]
        if not isinstance(raw_inputs, list):
            raise ContractError("inputs must be an array")
        inputs: list[InputRef] = []
        for raw in raw_inputs:
            item = _mapping(raw, "inputs[]")
            inputs.append(
                InputRef(
                    role=_string(item.get("role"), "inputs.role"),
                    artifact_id=_optional_string(item.get("artifact_id"), "artifact_id"),
                    source_asset_id=_optional_string(
                        item.get("source_asset_id"), "source_asset_id"
                    ),
                    coordinate_range=(
                        _mapping(item["coordinate_range"], "coordinate_range")
                        if "coordinate_range" in item
                        else None
                    ),
                )
            )
        envelope = cls(
            _string(value["schema_version"], "schema_version"),
            _string(value["artifact_id"], "artifact_id"),
            _string(value["artifact_kind"], "artifact_kind"),
            _string(value["scope_key"], "scope_key"),
            _string(value["content_hash"], "content_hash"),
            _string(value["created_at"], "created_at"),
            producer,
            tuple(inputs),
            _mapping(value["payload"], "payload"),
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
        if self.schema_version != SCHEMA_VERSION:
            raise ContractError(
                f"unsupported schema_version: expected {SCHEMA_VERSION}, "
                f"found {self.schema_version}"
            )
        validate_scope(self.artifact_kind, self.scope_key, self.payload)
        validate_payload(self.artifact_kind, self.payload)
        expected = artifact_content_hash(
            artifact_kind=self.artifact_kind,
            scope_key=self.scope_key,
            schema_version=self.schema_version,
            producer=self.producer.as_dict(),
            inputs=[item.as_dict() for item in self.inputs],
            payload=self.payload,
        )
        if expected != self.content_hash:
            raise ContractError("artifact content_hash does not match semantic content")
        if self.artifact_id != "art_" + expected.removeprefix("sha256:"):
            raise ContractError("artifact_id does not match content_hash")


def validate_scope(kind: str, scope_key: str, payload: Mapping[str, Any]) -> None:
    if kind not in ARTIFACT_KINDS:
        raise ContractError(f"unknown artifact kind: {kind}")
    if kind in SCOPED_KINDS:
        field = "disagreement_id" if kind == "acoustic_resolution" else "window_id"
        if payload.get(field) != scope_key:
            raise ContractError(f"{kind} scope_key must equal payload.{field}")
    elif scope_key != "global":
        raise ContractError(f"{kind} must use global scope_key")


def validate_payload(kind: str, payload: Mapping[str, Any]) -> None:
    if kind == "job_input":
        validate_job_input_payload(payload)
    elif kind == "media_probe":
        _positive_int(payload.get("presentation_duration_ms"), "presentation_duration_ms")
        _positive_int(payload.get("presentation_total_samples"), "presentation_total_samples")
        if payload.get("timeline_status") not in {"normal", "corrected", "unverified"}:
            raise ContractError("invalid media_probe timeline_status")
    elif kind == "timeline_audio":
        _string(payload.get("source_asset_id"), "source_asset_id")
        _positive_int(payload.get("duration_ms"), "duration_ms")
        _positive_int(payload.get("total_sample_count"), "total_sample_count")
        _blob(payload.get("audio_blob"), "timeline_audio.audio_blob")
    elif kind == "media_object":
        for name in ("source_asset_id", "provider", "bucket", "object_key", "content_hash"):
            _string(payload.get(name), f"media_object.{name}")
        _positive_int(payload.get("byte_length"), "media_object.byte_length")
        if "get_url" in payload:
            raise ContractError("media_object must not persist a presigned GET URL")
    elif kind in {"base_asr", "peer_asr", "glm_adjudication_evidence"}:
        _string(payload.get("source_text"), f"{kind}.source_text")
        _timed_units(payload.get("timed_units"), f"{kind}.timed_units")
        _provider_metadata(payload.get("provider_metadata"), f"{kind}.provider_metadata")
        if kind == "glm_adjudication_evidence":
            _string(payload.get("window_id"), "glm_adjudication_evidence.window_id")
    elif kind == "asr_comparison":
        _hunks(payload.get("hunks"))
    elif kind == "acoustic_window":
        _string(payload.get("window_id"), "window_id")
        start, end = _interval(payload, "global_start_ms", "global_end_ms")
        blob = _blob(payload.get("audio_blob"), "acoustic_window.audio_blob")
        if end - start > 30_000:
            raise ContractError("GLM evidence window exceeds 30 seconds")
        if int(blob["byte_length"]) > 25_000_000:
            raise ContractError("GLM evidence window exceeds 25 MB")
    elif kind in {"qwen_edit_proposal", "kimi_edit_proposal"}:
        _edits(payload.get("edits"), f"{kind}.edits")
        _provider_metadata(payload.get("provider_metadata"), f"{kind}.provider_metadata")
    elif kind == "edit_proposal":
        proposals = _mapping(payload.get("proposals"), "edit_proposal.proposals")
        metadata = _mapping(payload.get("provider_metadata"), "provider_metadata")
        if set(proposals) != {"qwen", "kimi"} or set(metadata) != {"qwen", "kimi"}:
            raise ContractError("edit_proposal requires qwen and kimi arms")
        for arm, value in proposals.items():
            _edits(value, f"proposals.{arm}")
            _provider_metadata(metadata[arm], f"provider_metadata.{arm}")
    elif kind == "acoustic_window_plan":
        _string(payload.get("run_id"), "run_id")
        planned = payload.get("disagreement_ids")
        if not isinstance(planned, list) or len(planned) != len(set(planned)):
            raise ContractError("window plan requires unique disagreement IDs")
        covered: list[str] = []
        for window in payload.get("windows", []):
            item = _mapping(window, "windows[]")
            start, end = _interval(item, "global_start_ms", "global_end_ms")
            if end - start > 30_000:
                raise ContractError("planned window exceeds 30 seconds")
            _string(item.get("window_id"), "window_id")
            covered.extend(item["disagreement_ids"])
        covered.extend(item["disagreement_id"] for item in payload.get("unavailable", []))
        if sorted(covered) != sorted(planned):
            raise ContractError("window plan does not exactly partition disagreements")
    elif kind == "acoustic_resolution":
        _string(payload.get("disagreement_id"), "disagreement_id")
        _string(payload.get("reason"), "reason")
        if payload.get("status") not in {"resolved", "review"}:
            raise ContractError("invalid acoustic resolution status")
        if payload["status"] == "resolved":
            _string(payload.get("selected_text"), "selected_text", allow_empty=True)
            _string(payload.get("evidence_artifact_id"), "evidence_artifact_id")
    elif kind == "review_resolution":
        _string(payload.get("run_id"), "run_id")
        _string(payload.get("queue_artifact_id"), "queue_artifact_id")
        if not isinstance(payload.get("decisions"), list):
            raise ContractError("review decisions are missing")
    elif kind in {"agreement_resolution", "edit_resolution"}:
        _string(payload.get("base_text"), "edit_resolution.base_text")
        resolved = payload.get("resolved_edits")
        reviews = payload.get("review_items")
        if not isinstance(resolved, list) or not isinstance(reviews, list):
            raise ContractError("edit resolution arrays are missing")
        for value in resolved:
            item = _mapping(value, "resolved_edits[]")
            _non_negative_int(item.get("start"), "resolved_edit.start")
            _non_negative_int(item.get("end"), "resolved_edit.end")
            _string(item.get("replacement"), "resolved_edit.replacement", allow_empty=True)
            if item.get("resolution") == "lexical_agreement_ignore_prosody":
                from cueflow.edit_resolution import (
                    locate_edit,
                    parse_edits_json,
                    project_lexical_changes,
                )

                support = _mapping(item.get("support"), "projection.support")
                for arm in ("qwen", "kimi"):
                    edits = parse_edits_json({"edits": support.get(arm)})
                    if len(edits) != 1:
                        raise ContractError("projection agreement requires one edit per arm")
                    located = locate_edit(str(payload["base_text"]), edits[0])
                    if (located.start, located.end) != (item["start"], item["end"]):
                        raise ContractError(
                            "projection agreement cannot merge different Base spans"
                        )
                    projection = project_lexical_changes(str(payload["base_text"]), located)
                    if projection.get("text") != item["replacement"]:
                        raise ContractError("projection agreement cannot invent lexical content")
                    stored = _mapping(support.get("projections"), "projection.projections")
                    arm_projection = _mapping(stored.get(arm), f"projection.{arm}")
                    if arm_projection != {
                        "status": "resolved",
                        "text": projection["text"],
                        "parts": [projection],
                    }:
                        raise ContractError("stored projection provenance does not recompute")
        from cueflow.edit_resolution import apply_resolved_payload

        rebuilt = apply_resolved_payload(str(payload["base_text"]), resolved)
        if payload.get("corrected_preview") != rebuilt:
            raise ContractError("resolution preview does not rebuild from Base")
        if kind == "agreement_resolution":
            for field in ("lexical_disagreements", "ignored_disagreements"):
                if not isinstance(payload.get(field), list):
                    raise ContractError(f"agreement resolution missing {field}")
        else:
            _string(payload.get("run_id"), "run_id")
            pending = _non_negative_int(payload.get("pending_acoustic"), "pending_acoustic")
            if not isinstance(payload.get("sealed"), bool):
                raise ContractError("resolution.sealed must be boolean")
            if payload["sealed"] and (pending or reviews):
                raise ContractError("sealed resolution has pending work")
    elif kind == "review_queue":
        _string(payload.get("run_id"), "run_id")
        if not isinstance(payload.get("items"), list):
            raise ContractError("review_queue.items must be an array")
        if payload.get("status") not in {"clear", "needs_review", "resolved"}:
            raise ContractError("invalid review_queue status")
    elif kind == "transcript":
        validate_transcript_payload(payload)
    elif kind == "alignment":
        validate_alignment_payload(payload)
    elif kind == "subtitle":
        validate_subtitle_payload(payload)
    elif kind == "qa":
        validate_qa_payload(payload)
    elif kind == "srt_render":
        _string(payload.get("subtitle_artifact_id"), "subtitle_artifact_id")
        _string(payload.get("qa_artifact_id"), "qa_artifact_id")
        _string(payload.get("text"), "srt_render.text", allow_empty=True)


def validate_job_input_payload(payload: Mapping[str, Any]) -> None:
    _string(payload.get("source_asset_id"), "job_input.source_asset_id")
    references = payload.get("references")
    if not isinstance(references, list):
        raise ContractError("job_input.references must be an array")
    for ordinal, raw in enumerate(references):
        item = _mapping(raw, "job_input.references[]")
        if _non_negative_int(item.get("ordinal"), "reference.ordinal") != ordinal:
            raise ContractError("Reference ordinals must be contiguous")
        kind = item.get("kind")
        _string(item.get("display_name"), "reference.display_name")
        if kind in URL_REFERENCE_KINDS:
            parsed = urlsplit(_string(item.get("url"), "reference.url"))
            if parsed.scheme != "https" or not parsed.netloc:
                raise ContractError("PDF and image References require an absolute HTTPS URL")
            if item.get("locator_semantics") != "mutable_remote_locator":
                raise ContractError("URL Reference must declare mutable locator semantics")
        elif kind == "text":
            if item.get("format") not in TEXT_REFERENCE_FORMATS:
                raise ContractError("unsupported text Reference format")
            _string(item.get("text"), "reference.text")
        else:
            raise ContractError("unsupported Reference kind")
    keywords = payload.get("user_keywords")
    if not isinstance(keywords, list) or any(
        not isinstance(item, str) or not item for item in keywords
    ):
        raise ContractError("job_input.user_keywords must contain non-empty strings")
    if len(keywords) > MAX_USER_KEYWORDS or len(keywords) != len(set(keywords)):
        raise ContractError("job_input.user_keywords exceeds limit or contains duplicates")


def validate_transcript_payload(payload: Mapping[str, Any]) -> None:
    source_text = _string(payload.get("source_text"), "source_text")
    _string(payload.get("base_asr_artifact_id"), "base_asr_artifact_id")
    _string(payload.get("edit_resolution_artifact_id"), "edit_resolution_artifact_id")
    if payload.get("correction_mode") != "post_correction_adjudication":
        raise ContractError("invalid transcript.correction_mode")
    leading = _string(payload.get("leading_decoration", ""), "leading_decoration", allow_empty=True)
    atoms = payload.get("atoms")
    if (
        not isinstance(atoms, list)
        or not atoms
        or payload.get("atomizer_version") != ATOMIZER_VERSION
    ):
        raise ContractError("transcript atoms or atomizer version are invalid")
    rebuilt = leading
    for position, raw in enumerate(atoms):
        atom = _mapping(raw, "atoms[]")
        if atom.get("position") != position or atom.get("atom_class") not in ATOM_CLASSES:
            raise ContractError("invalid transcript atom order or class")
        _string(atom.get("atom_id"), "atom_id")
        rebuilt += _string(atom.get("text"), "atom.text")
        rebuilt += _string(atom.get("decoration_after", ""), "decoration_after", allow_empty=True)
    if rebuilt != source_text:
        raise ContractError("transcript atoms do not rebuild source_text")


def validate_alignment_payload(payload: Mapping[str, Any]) -> None:
    _string(payload.get("transcript_artifact_id"), "transcript_artifact_id")
    _string(payload.get("media_object_artifact_id"), "media_object_artifact_id")
    _string(payload.get("timeline_audio_artifact_id"), "timeline_audio_artifact_id")
    duration = _positive_int(payload.get("duration_ms"), "alignment.duration_ms")
    if payload.get("provider_coordinate_system") != "global_milliseconds":
        raise ContractError("invalid alignment coordinate system")
    assignments = payload.get("assignments")
    if not isinstance(assignments, list) or not assignments:
        raise ContractError("alignment.assignments must be a non-empty array")
    previous_end = -1
    for raw in assignments:
        item = _mapping(raw, "assignments[]")
        _string(item.get("atom_id"), "assignment.atom_id")
        start, end = _interval(item, "global_start_ms", "global_end_ms")
        if start < previous_end or end > duration:
            raise ContractError("alignment assignments overlap or exceed duration")
        previous_end = end


def validate_alignment_against(
    alignment: Mapping[str, Any], transcript: Mapping[str, Any], duration_ms: int
) -> None:
    assignments = alignment.get("assignments")
    if not isinstance(assignments, list):
        raise ContractError("alignment assignments missing")
    actual = [
        _string(_mapping(item, "assignments[]").get("atom_id"), "atom_id") for item in assignments
    ]
    if actual != atom_ids(transcript) or alignment.get("duration_ms") != duration_ms:
        raise ContractError("alignment does not exactly cover the corrected transcript")


def validate_subtitle_payload(payload: Mapping[str, Any]) -> None:
    _string(payload.get("transcript_artifact_id"), "subtitle.transcript_artifact_id")
    _string(payload.get("alignment_artifact_id"), "subtitle.alignment_artifact_id")
    _string(payload.get("segmenter_config_hash"), "segmenter_config_hash")
    duration = _positive_int(payload.get("duration_ms"), "subtitle.duration_ms")
    cues = payload.get("cues")
    if not isinstance(cues, list):
        raise ContractError("subtitle.cues must be an array")
    previous_end = -1
    for raw in cues:
        cue = _mapping(raw, "cues[]")
        _string(cue.get("cue_id"), "cue_id")
        start, end = _interval(cue, "global_start_ms", "global_end_ms")
        if start < previous_end or end > duration:
            raise ContractError("subtitle cues overlap or exceed duration")
        previous_end = end
        _string(cue.get("text"), "cue.text")
        count = _positive_int(cue.get("display_unit_count"), "display_unit_count")
        refs = cue.get("atom_refs")
        if not isinstance(refs, list) or len(refs) != count:
            raise ContractError("cue atom_refs must exactly cover display units")


def validate_qa_payload(payload: Mapping[str, Any]) -> None:
    if payload.get("qa_ruleset_version") != "0.2.0":
        raise ContractError("unsupported QA ruleset version")
    if payload.get("result") not in {"passed", "warnings", "blocked"}:
        raise ContractError("invalid QA result")
    if not isinstance(payload.get("subject_artifact_ids"), list):
        raise ContractError("qa.subject_artifact_ids must be an array")
    if not isinstance(payload.get("issues"), list):
        raise ContractError("qa.issues must be an array")


def atom_ids(payload: Mapping[str, Any]) -> list[str]:
    atoms = payload.get("atoms")
    if not isinstance(atoms, list):
        raise ContractError("transcript atoms missing")
    return [_string(_mapping(atom, "atoms[]").get("atom_id"), "atom_id") for atom in atoms]


def _provider_metadata(value: Any, name: str) -> None:
    item = _mapping(value, name)
    for field in ("provider", "requested_model"):
        _string(item.get(field), f"{name}.{field}")
    _optional_string(item.get("resolved_model"), f"{name}.resolved_model")
    _optional_string(item.get("response_id"), f"{name}.response_id")
    for field in ("elapsed_ms", "reasoning_ms"):
        if item.get(field) is not None:
            _non_negative_int(item[field], f"{name}.{field}")


def _timed_units(value: Any, name: str) -> None:
    if not isinstance(value, list):
        raise ContractError(f"{name} must be an array")
    previous = -1
    for raw in value:
        item = _mapping(raw, f"{name}[]")
        _string(item.get("text"), f"{name}.text")
        start, end = _interval(item, "start_ms", "end_ms")
        if start < previous:
            raise ContractError(f"{name} must be ordered and non-overlapping")
        previous = end


def _hunks(value: Any) -> None:
    if not isinstance(value, list):
        raise ContractError("asr_comparison.hunks must be an array")
    for raw in value:
        item = _mapping(raw, "hunks[]")
        for field in ("base_start", "base_end", "peer_start", "peer_end"):
            _non_negative_int(item.get(field), f"hunk.{field}")
        _string(item.get("base_text"), "hunk.base_text", allow_empty=True)
        _string(item.get("peer_text"), "hunk.peer_text", allow_empty=True)
        if item.get("category") not in {"prosodic_format_only", "lexical"}:
            raise ContractError("invalid hunk category")


def _edits(value: Any, name: str) -> None:
    if not isinstance(value, list):
        raise ContractError(f"{name} must be an array")
    for raw in value:
        item = _mapping(raw, f"{name}[]")
        if set(item) != {"source_sentence", "original", "replacement"}:
            raise ContractError("edit fields do not match the current contract")
        _string(item["source_sentence"], "source_sentence")
        _string(item["original"], "original")
        _string(item["replacement"], "replacement", allow_empty=True)


def _blob(value: Any, name: str) -> Mapping[str, Any]:
    item = _mapping(value, name)
    _string(item.get("content_hash"), f"{name}.content_hash")
    _positive_int(item.get("byte_length"), f"{name}.byte_length")
    _string(item.get("media_type"), f"{name}.media_type")
    return item


def _interval(value: Mapping[str, Any], start_name: str, end_name: str) -> tuple[int, int]:
    start = _non_negative_int(value.get(start_name), start_name)
    end = _positive_int(value.get(end_name), end_name)
    if end <= start:
        raise ContractError(f"{end_name} must be greater than {start_name}")
    return start, end


def _mapping(value: Any, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ContractError(f"{name} must be an object")
    return value


def _string(value: Any, name: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or (not allow_empty and not value):
        raise ContractError(f"{name} must be a string")
    return value


def _optional_string(value: Any, name: str) -> str | None:
    if value is None:
        return None
    return _string(value, name)


def _non_negative_int(value: Any, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ContractError(f"{name} must be a non-negative integer")
    return value


def _positive_int(value: Any, name: str) -> int:
    result = _non_negative_int(value, name)
    if result == 0:
        raise ContractError(f"{name} must be positive")
    return result
