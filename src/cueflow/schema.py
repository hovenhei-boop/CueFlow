from __future__ import annotations

import unicodedata
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
        "chunk_plan",
        "media_chunk",
        "system_glossary",
        "project_glossary",
        "effective_glossary",
        "transcript",
        "alignment",
        "subtitle",
        "qa",
        "srt_render",
        "reference_input",
        "reference_evidence",
        "reference_bundle",
        "lexicon_input",
        "term_candidate_set",
        "project_lexicon",
    }
)
CHUNK_KINDS = frozenset({"media_chunk", "transcript", "alignment"})
GLOSSARY_KINDS = frozenset({"system_glossary", "project_glossary", "effective_glossary"})
REFERENCE_KINDS = frozenset(
    {"reference_input", "reference_evidence", "reference_bundle"}
)
LEXICON_RUN_KINDS = frozenset({"lexicon_input", "term_candidate_set"})
TERM_CATEGORIES = frozenset({"proper_noun", "noun_or_term", "verb", "other"})
PROPER_NOUN_SUBTYPES = frozenset(
    {
        "person",
        "organization",
        "location",
        "event",
        "project_or_program",
        "product_brand_model_software",
        "standard_protocol_code",
        "work_or_title",
        "other",
    }
)
REFERENCE_EVIDENCE_ROLES = frozenset(
    {
        "text_subtitle",
        "bitmap_subtitle",
        "burned_subtitle",
        "cloud_reference_asr",
        "document_text",
        "cloud_document_parse",
        "image_visual",
    }
)
ATOM_CLASSES = frozenset({"word", "cjk_character", "number", "pronounceable_symbol"})
INT64_MAX = 2**63 - 1


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
    reference_asset_id: str | None = None
    coordinate_range: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        identities = (self.artifact_id, self.source_asset_id, self.reference_asset_id)
        if sum(value is not None for value in identities) != 1:
            raise ContractError(
                "InputRef requires exactly one artifact_id, source_asset_id, "
                "or reference_asset_id"
            )

    def as_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {"role": self.role}
        if self.artifact_id is not None:
            result["artifact_id"] = self.artifact_id
        if self.source_asset_id is not None:
            result["source_asset_id"] = self.source_asset_id
        if self.reference_asset_id is not None:
            result["reference_asset_id"] = self.reference_asset_id
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
        schema_version = _string(value.get("schema_version"), "schema_version")
        _validate_schema_version(schema_version)
        producer_value = _mapping(value.get("producer"), "producer")
        if set(producer_value) != {
            "component", "component_version", "provider", "model", "config_hash"
        }:
            raise ContractError("producer fields do not match the current schema")
        producer = Producer(
            component=_string(producer_value.get("component"), "producer.component"),
            component_version=_string(
                producer_value.get("component_version"), "producer.component_version"
            ),
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
                reference_asset_id=_optional_string(item.get("reference_asset_id")),
                coordinate_range=item.get("coordinate_range"),
            )
            for raw in inputs_value
            for item in [_mapping(raw, "inputs[]")]
        )
        envelope = cls(
            schema_version=schema_version,
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
        _validate_schema_version(self.schema_version)
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
    elif kind in REFERENCE_KINDS:
        reference_asset_id = payload.get("reference_asset_id")
        if not isinstance(reference_asset_id, str) or reference_asset_id != scope_key:
            raise ContractError(f"{kind} scope_key must equal payload.reference_asset_id")
    elif kind == "lexicon_input":
        if payload.get("run_id") != scope_key:
            raise ContractError("lexicon_input scope_key must equal payload.run_id")
    elif kind == "term_candidate_set":
        if payload.get("work_item_id") != scope_key:
            raise ContractError(
                "term_candidate_set scope_key must equal payload.work_item_id"
            )
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
    elif kind == "media_chunk":
        validate_interval(payload, "global_start_ms", "global_end_ms")
        _string(payload.get("timeline_audio_artifact_id"), "timeline_audio_artifact_id")
    elif kind == "chunk_plan":
        chunks = payload.get("chunks")
        if not isinstance(chunks, list) or not chunks:
            raise ContractError("chunk_plan.chunks must be a non-empty array")
        _string(payload.get("timeline_audio_artifact_id"), "timeline_audio_artifact_id")
        config = _mapping(payload.get("config"), "chunk_plan.config")
        target = _positive_integer(config.get("target_duration_ms"), "target_duration_ms")
        hard_limit = _positive_integer(config.get("hard_limit_ms"), "hard_limit_ms")
        _positive_integer(config.get("silence_min_duration_ms"), "silence_min_duration_ms")
        if target > hard_limit:
            raise ContractError("chunk target duration cannot exceed hard limit")
        _validate_chunk_coverage(
            chunks,
            _positive_integer(payload.get("duration_ms"), "duration_ms"),
            hard_limit,
        )
    elif kind == "timeline_audio":
        duration_ms = _positive_integer(payload.get("duration_ms"), "duration_ms")
        total_samples = _positive_integer(payload.get("total_sample_count"), "total_sample_count")
        if (
            payload.get("sample_rate_hz") != 16_000
            or payload.get("channels") != 1
            or payload.get("sample_format") != "s16le"
        ):
            raise ContractError("timeline_audio must be 16kHz mono PCM s16le")
        if payload.get("timeline_origin_sample") != 0:
            raise ContractError("timeline_audio origin must be sample zero")
        expected_ms = (total_samples * 1000 + 8000) // 16_000
        if duration_ms != expected_ms:
            raise ContractError("timeline_audio duration does not match total sample count")
    elif kind == "media_probe":
        if payload.get("timeline_status") not in {"normal", "corrected", "unverified"}:
            raise ContractError("invalid media_probe timeline_status")
        _positive_integer(payload.get("presentation_duration_ms"), "presentation_duration_ms")
        _positive_integer(payload.get("presentation_total_samples"), "presentation_total_samples")
        _positive_integer(payload.get("opening_scan_limit_ms"), "opening_scan_limit_ms")
        _validate_media_probe_evidence(payload)
        _validate_timeline_actions(payload.get("timeline_actions"))
    elif kind == "srt_render":
        _string(payload.get("subtitle_artifact_id"), "subtitle_artifact_id")
        _string(payload.get("qa_artifact_id"), "qa_artifact_id")
        if not isinstance(payload.get("text"), str):
            raise ContractError("srt_render.text must be a string")
    elif kind == "reference_input":
        validate_reference_input_payload(payload)
    elif kind == "reference_evidence":
        validate_reference_evidence_payload(payload)
    elif kind == "reference_bundle":
        validate_reference_bundle_payload(payload)
    elif kind == "lexicon_input":
        validate_lexicon_input_payload(payload)
    elif kind == "term_candidate_set":
        validate_term_candidate_set_payload(payload)
    elif kind == "project_lexicon":
        validate_project_lexicon_payload(payload)


def validate_reference_input_payload(payload: Mapping[str, Any]) -> None:
    _string(payload.get("reference_asset_id"), "reference_input.reference_asset_id")
    _string(payload.get("run_id"), "reference_input.run_id")
    _string(payload.get("work_item_id"), "reference_input.work_item_id")
    _string(payload.get("input_kind"), "reference_input.input_kind")
    _string(payload.get("branch"), "reference_input.branch")
    _string(payload.get("detected_format"), "reference_input.detected_format")
    _nullable_number(
        payload.get("local_measured_duration"),
        "reference_input.local_measured_duration",
    )
    _mapping(payload.get("manifest"), "reference_input.manifest")


def validate_reference_evidence_payload(payload: Mapping[str, Any]) -> None:
    _string(payload.get("reference_asset_id"), "reference_evidence.reference_asset_id")
    _string(payload.get("run_id"), "reference_evidence.run_id")
    _string(payload.get("work_item_id"), "reference_evidence.work_item_id")
    role = _string(payload.get("evidence_role"), "reference_evidence.evidence_role")
    if role not in REFERENCE_EVIDENCE_ROLES:
        raise ContractError(f"unsupported Reference evidence role: {role}")
    _string(payload.get("branch"), "reference_evidence.branch")
    if "content" not in payload:
        raise ContractError("reference_evidence.content is required")
    _mapping(payload.get("provenance"), "reference_evidence.provenance")
    _nullable_number(
        payload.get("local_measured_duration"),
        "reference_evidence.local_measured_duration",
    )
    _nullable_number(
        payload.get("provider_usage_duration"),
        "reference_evidence.provider_usage_duration",
    )
    provider_usage = payload.get("provider_usage")
    if provider_usage is not None:
        _mapping(provider_usage, "reference_evidence.provider_usage")
    _nullable_number(payload.get("provider_cost"), "reference_evidence.provider_cost")
    if "media_duration" in payload:
        raise ContractError(
            "Reference evidence must not use media_duration; use local_measured_duration"
        )


def validate_reference_bundle_payload(payload: Mapping[str, Any]) -> None:
    _string(payload.get("reference_asset_id"), "reference_bundle.reference_asset_id")
    _string(payload.get("run_id"), "reference_bundle.run_id")
    outcome = _string(payload.get("outcome"), "reference_bundle.outcome")
    if outcome not in {"complete", "partial"}:
        raise ContractError("Reference bundle outcome must be complete or partial")
    evidence_ids = payload.get("evidence_artifact_ids")
    if (
        not isinstance(evidence_ids, list)
        or not evidence_ids
        or any(not isinstance(value, str) or not value for value in evidence_ids)
    ):
        raise ContractError("Reference bundle requires evidence Artifact ids")
    failures = payload.get("failures")
    if not isinstance(failures, list) or any(not isinstance(value, Mapping) for value in failures):
        raise ContractError("reference_bundle.failures must be an array of objects")


def validate_lexicon_input_payload(payload: Mapping[str, Any]) -> None:
    _string(payload.get("run_id"), "lexicon_input.run_id")
    _string(
        payload.get("trigger_reference_run_id"),
        "lexicon_input.trigger_reference_run_id",
    )
    _string(
        payload.get("reference_bundle_artifact_id"),
        "lexicon_input.reference_bundle_artifact_id",
    )
    if payload.get("normalization_version") != "0.1.0":
        raise ContractError("unsupported Lexicon normalization version")
    batches = payload.get("batches")
    if not isinstance(batches, list) or not batches:
        raise ContractError("lexicon_input.batches must be a non-empty array")
    seen_items: set[str] = set()
    for raw in batches:
        batch = _mapping(raw, "lexicon_input.batches[]")
        work_item_id = _string(batch.get("work_item_id"), "work_item_id")
        if work_item_id in seen_items:
            raise ContractError("lexicon_input contains duplicate work item IDs")
        seen_items.add(work_item_id)
        _string(batch.get("evidence_artifact_id"), "evidence_artifact_id")
        _non_negative_integer(batch.get("batch_ordinal"), "batch_ordinal")
        units = batch.get("units")
        if not isinstance(units, list) or not units:
            raise ContractError("lexicon_input batch requires Evidence units")
        for raw_unit in units:
            unit = _mapping(raw_unit, "lexicon_input.units[]")
            path = unit.get("field_path")
            if not isinstance(path, list) or not path:
                raise ContractError("lexicon_input unit.field_path must be a non-empty array")
            start = _non_negative_integer(
                unit.get("source_start_offset"), "source_start_offset"
            )
            end = _positive_integer(unit.get("source_end_offset"), "source_end_offset")
            if end <= start:
                raise ContractError("lexicon_input unit source interval is invalid")
            _string(unit.get("text_hash"), "lexicon_input unit.text_hash")
            _mapping(unit.get("coordinates"), "lexicon_input unit.coordinates")


def validate_term_candidate_set_payload(payload: Mapping[str, Any]) -> None:
    _string(payload.get("run_id"), "term_candidate_set.run_id")
    _string(payload.get("work_item_id"), "term_candidate_set.work_item_id")
    _string(
        payload.get("evidence_artifact_id"),
        "term_candidate_set.evidence_artifact_id",
    )
    candidates = payload.get("candidates")
    if not isinstance(candidates, list):
        raise ContractError("term_candidate_set.candidates must be an array")
    for raw in candidates:
        candidate = _mapping(raw, "term_candidate_set.candidates[]")
        if set(candidate) != {
            "candidate_id",
            "normalized_surface_form",
            "display_term",
            "display_category",
            "display_proper_noun_subtype",
            "disposition",
            "occurrences",
        }:
            raise ContractError("term_candidate_set candidate fields are invalid")
        disposition = _string(candidate.get("disposition"), "disposition")
        if disposition not in {
            "suggested",
            "already_in_project_lexicon",
            "suppressed_blacklist",
            "suppressed_trash",
            "suppressed_blacklist_and_trash",
        }:
            raise ContractError("term_candidate_set has invalid disposition")
        candidate_id = candidate.get("candidate_id")
        if disposition == "suggested":
            _string(candidate_id, "candidate_id")
        elif candidate_id is not None:
            raise ContractError("suppressed term_candidate_set item cannot have candidate_id")
        _string(candidate.get("normalized_surface_form"), "normalized_surface_form")
        _string(candidate.get("display_term"), "display_term")
        _validate_term_category(
            candidate.get("display_category"),
            candidate.get("display_proper_noun_subtype"),
            "term_candidate_set display category",
        )
        occurrences = candidate.get("occurrences")
        if not isinstance(occurrences, list) or not occurrences:
            raise ContractError("term_candidate_set candidate requires occurrences")
        for raw_occurrence in occurrences:
            occurrence = _mapping(raw_occurrence, "term_candidate_set.occurrences[]")
            if set(occurrence) != {
                "raw_surface_form",
                "suggested_surface_form",
                "field_path",
                "start_offset",
                "end_offset",
                "category",
                "proper_noun_subtype",
                "risk_tags",
                "context_before",
                "context_after",
                "coordinates",
            }:
                raise ContractError("term_candidate_set occurrence fields are invalid")
            _string(occurrence.get("raw_surface_form"), "raw_surface_form")
            suggested = occurrence.get("suggested_surface_form")
            if suggested is not None:
                _string(suggested, "suggested_surface_form")
            path = occurrence.get("field_path")
            if (
                not isinstance(path, list)
                or not path
                or any(
                    isinstance(part, bool) or not isinstance(part, (str, int))
                    for part in path
                )
            ):
                raise ContractError("term_candidate_set occurrence field_path is invalid")
            start = _non_negative_integer(occurrence.get("start_offset"), "start_offset")
            end = _positive_integer(occurrence.get("end_offset"), "end_offset")
            if end <= start:
                raise ContractError("term_candidate_set occurrence interval is invalid")
            _validate_term_category(
                occurrence.get("category"),
                occurrence.get("proper_noun_subtype"),
                "term_candidate_set occurrence category",
            )
            risk_tags = occurrence.get("risk_tags")
            if not isinstance(risk_tags, list) or any(
                not isinstance(tag, str) or not tag for tag in risk_tags
            ):
                raise ContractError("term_candidate_set risk_tags are invalid")
            _string(occurrence.get("context_before"), "context_before", allow_empty=True)
            _string(occurrence.get("context_after"), "context_after", allow_empty=True)
            _mapping(occurrence.get("coordinates"), "coordinates")


def validate_project_lexicon_payload(payload: Mapping[str, Any]) -> None:
    if set(payload) != {
        "revision_id",
        "ordinal",
        "parent_revision_id",
        "decision_id",
        "entries",
    }:
        raise ContractError("project_lexicon fields are invalid")
    _string(payload.get("revision_id"), "project_lexicon.revision_id")
    _positive_integer(payload.get("ordinal"), "project_lexicon.ordinal")
    parent = payload.get("parent_revision_id")
    if parent is not None:
        _string(parent, "project_lexicon.parent_revision_id")
    _string(payload.get("decision_id"), "project_lexicon.decision_id")
    entries = payload.get("entries")
    if not isinstance(entries, list):
        raise ContractError("project_lexicon.entries must be an array")
    seen: set[str] = set()
    for raw in entries:
        entry = _mapping(raw, "project_lexicon.entries[]")
        if set(entry) != {
            "entry_id",
            "term",
            "category",
            "proper_noun_subtype",
            "enabled",
            "entry_revision",
        }:
            raise ContractError("project_lexicon entry fields are invalid")
        _string(entry.get("entry_id"), "entry_id")
        term = _string(entry.get("term"), "term")
        normalized = unicodedata.normalize("NFC", term).strip()
        if normalized in seen:
            raise ContractError("project_lexicon terms must be unique")
        seen.add(normalized)
        _validate_term_category(
            entry.get("category"),
            entry.get("proper_noun_subtype"),
            "project_lexicon category",
        )
        if not isinstance(entry.get("enabled"), bool):
            raise ContractError("project_lexicon entry.enabled must be boolean")
        _positive_integer(entry.get("entry_revision"), "entry_revision")


def _validate_term_category(category: Any, subtype: Any, name: str) -> None:
    if category not in TERM_CATEGORIES:
        raise ContractError(f"{name} is invalid")
    if category == "proper_noun":
        if subtype not in PROPER_NOUN_SUBTYPES:
            raise ContractError(f"{name} requires a supported proper noun subtype")
    elif subtype is not None:
        raise ContractError(f"{name} cannot have a proper noun subtype")


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
    if payload.get("qa_ruleset_version") != "0.1.1":
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


def _validate_timeline_actions(value: Any) -> None:
    if not isinstance(value, list):
        raise ContractError("media_probe.timeline_actions must be an array")
    origin_actions = {
        "timeline_origin_unchanged",
        "pad_silence_before",
        "trim_before_timeline",
        "timeline_origin_unverified",
    }
    seen_origin = 0
    seen_duration = 0
    for raw in value:
        action = _mapping(raw, "timeline_actions[]")
        name = _string(action.get("action"), "timeline action")
        if name in origin_actions:
            seen_origin += 1
            if name in {"pad_silence_before", "trim_before_timeline"}:
                _positive_integer(action.get("sample_count"), "sample_count")
            elif "sample_count" in action:
                raise ContractError(f"{name} must not carry sample_count")
        elif name == "fit_presentation_duration":
            seen_duration += 1
            _positive_integer(action.get("total_sample_count"), "total_sample_count")
        else:
            raise ContractError(f"unknown timeline action: {name}")
    if seen_origin != 1 or seen_duration != 1:
        raise ContractError("timeline actions require one origin and one duration action")


def _validate_media_probe_evidence(payload: Mapping[str, Any]) -> None:
    _require_fields(
        payload,
        (
            "media_kind",
            "container",
            "video_stream",
            "audio_stream",
            "presentation_evidence",
            "continuity_check",
            "timeline_issues",
        ),
        "media_probe",
    )
    if payload.get("media_kind") not in {"audio", "video"}:
        raise ContractError("invalid media_probe media_kind")

    container = _mapping(payload.get("container"), "media_probe.container")
    _require_fields(container, ("format_name", "start_time", "duration"), "container")
    _string(container.get("format_name"), "container.format_name")
    _optional_string(container.get("start_time"))
    _optional_string(container.get("duration"))

    _validate_stream_facts(payload.get("audio_stream"), "audio_stream")
    video_stream = payload.get("video_stream")
    if video_stream is not None:
        _validate_stream_facts(video_stream, "video_stream")

    evidence = _mapping(
        payload.get("presentation_evidence"), "media_probe.presentation_evidence"
    )
    _require_fields(
        evidence,
        ("media_origin", "audio_start", "exact_offset"),
        "presentation_evidence",
    )
    _validate_frame_evidence(evidence.get("media_origin"), "media_origin")
    _validate_frame_evidence(evidence.get("audio_start"), "audio_start")
    _validate_fraction_evidence(evidence.get("exact_offset"), "exact_offset")
    _validate_continuity_evidence(payload.get("continuity_check"))

    issues = payload.get("timeline_issues")
    if not isinstance(issues, list):
        raise ContractError("media_probe.timeline_issues must be an array")
    for issue in issues:
        _string(issue, "timeline_issues[]")


def _validate_stream_facts(value: Any, name: str) -> None:
    stream = _mapping(value, f"media_probe.{name}")
    _require_fields(
        stream,
        (
            "index",
            "codec_name",
            "start_pts",
            "duration_ts",
            "time_base_num",
            "time_base_den",
            "sample_rate_hz",
            "channels",
            "width",
            "height",
        ),
        name,
    )
    _nullable_non_negative_integer(stream.get("index"), f"{name}.index")
    _string(stream.get("codec_name"), f"{name}.codec_name")
    _nullable_integer(stream.get("start_pts"), f"{name}.start_pts")
    _nullable_integer(stream.get("duration_ts"), f"{name}.duration_ts")
    _validate_nullable_time_base(
        stream.get("time_base_num"), stream.get("time_base_den"), name
    )
    for field in ("sample_rate_hz", "channels", "width", "height"):
        _nullable_positive_integer(stream.get(field), f"{name}.{field}")


def _validate_frame_evidence(value: Any, name: str) -> None:
    if value is None:
        return
    frame = _mapping(value, f"presentation_evidence.{name}")
    _require_fields(
        frame,
        (
            "stream_index",
            "pts",
            "time_base_num",
            "time_base_den",
            "skip_samples",
            "nb_samples",
            "sample_rate_hz",
            "valid_start",
        ),
        name,
    )
    _non_negative_integer(frame.get("stream_index"), f"{name}.stream_index")
    _integer(frame.get("pts"), f"{name}.pts")
    _positive_integer(frame.get("time_base_num"), f"{name}.time_base_num")
    _positive_integer(frame.get("time_base_den"), f"{name}.time_base_den")
    _non_negative_integer(frame.get("skip_samples"), f"{name}.skip_samples")
    _nullable_non_negative_integer(frame.get("nb_samples"), f"{name}.nb_samples")
    _nullable_positive_integer(frame.get("sample_rate_hz"), f"{name}.sample_rate_hz")
    _validate_fraction_evidence(frame.get("valid_start"), f"{name}.valid_start", required=True)


def _validate_fraction_evidence(
    value: Any, name: str, *, required: bool = False
) -> None:
    if value is None and not required:
        return
    fraction = _mapping(value, name)
    _require_fields(fraction, ("numerator", "denominator"), name)
    _integer(fraction.get("numerator"), f"{name}.numerator")
    _positive_integer(fraction.get("denominator"), f"{name}.denominator")


def _validate_continuity_evidence(value: Any) -> None:
    continuity = _mapping(value, "media_probe.continuity_check")
    _require_fields(
        continuity,
        ("status", "packets_scanned", "first_anomaly"),
        "continuity_check",
    )
    status = continuity.get("status")
    if status not in {"continuous", "discontinuous", "unavailable"}:
        raise ContractError("invalid continuity_check.status")
    _non_negative_integer(continuity.get("packets_scanned"), "packets_scanned")
    anomaly_value = continuity.get("first_anomaly")
    if status == "continuous":
        if anomaly_value is not None:
            raise ContractError("continuous timeline must not contain first_anomaly")
        return
    anomaly = _mapping(anomaly_value, "continuity_check.first_anomaly")
    _string(anomaly.get("code"), "first_anomaly.code")
    if "packet_ordinal" in anomaly:
        _non_negative_integer(anomaly.get("packet_ordinal"), "first_anomaly.packet_ordinal")
    for field in ("expected", "observed"):
        if field in anomaly:
            _validate_fraction_evidence(anomaly.get(field), f"first_anomaly.{field}", required=True)


def _validate_nullable_time_base(numerator: Any, denominator: Any, name: str) -> None:
    if numerator is None and denominator is None:
        return
    if numerator is None or denominator is None:
        raise ContractError(f"{name} time base must provide numerator and denominator together")
    _positive_integer(numerator, f"{name}.time_base_num")
    _positive_integer(denominator, f"{name}.time_base_den")


def _require_fields(value: Mapping[str, Any], fields: Sequence[str], name: str) -> None:
    missing = [field for field in fields if field not in value]
    if missing:
        raise ContractError(f"{name} missing fields: {missing}")


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


def _validate_chunk_coverage(
    chunks: Iterable[Any], duration_ms: int, hard_limit_ms: int
) -> None:
    expected_start = 0
    for ordinal, raw in enumerate(chunks):
        chunk = _mapping(raw, "chunks[]")
        if _integer(chunk.get("ordinal"), "ordinal") != ordinal:
            raise ContractError("chunk ordinals must be contiguous")
        validate_interval(chunk, "global_start_ms", "global_end_ms")
        if chunk["global_start_ms"] != expected_start:
            raise ContractError("chunks must cover timeline without gaps or overlap")
        if chunk["global_end_ms"] - chunk["global_start_ms"] > hard_limit_ms:
            raise ContractError("chunk exceeds its plan hard limit")
        expected_start = chunk["global_end_ms"]
    if expected_start != duration_ms:
        raise ContractError("chunks do not cover the entire timeline")


def _validate_schema_version(version: str) -> None:
    parts = version.split(".")
    if len(parts) != 3 or not all(part.isdigit() for part in parts):
        raise ContractError("invalid schema version")
    if version != SCHEMA_VERSION:
        raise ContractError(
            f"incompatible schema version: expected {SCHEMA_VERSION}, found {version}"
        )


def _mapping(value: Any, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ContractError(f"{name} must be an object")
    return cast(Mapping[str, Any], value)


def _integer(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ContractError(f"{name} must be an integer")
    return cast(int, value)


def _positive_integer(value: Any, name: str) -> int:
    result = _integer(value, name)
    if result <= 0:
        raise ContractError(f"{name} must be positive")
    return result


def _non_negative_integer(value: Any, name: str) -> int:
    result = _integer(value, name)
    if result < 0:
        raise ContractError(f"{name} must be non-negative")
    return result


def _nullable_integer(value: Any, name: str) -> int | None:
    return None if value is None else _integer(value, name)


def _nullable_positive_integer(value: Any, name: str) -> int | None:
    return None if value is None else _positive_integer(value, name)


def _nullable_non_negative_integer(value: Any, name: str) -> int | None:
    return None if value is None else _non_negative_integer(value, name)


def _nullable_number(value: Any, name: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ContractError(f"{name} must be a number or null")
    result = float(value)
    if result < 0:
        raise ContractError(f"{name} must be non-negative")
    return result


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
