from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from cueflow.artifact_versions import ARTIFACT_KINDS
from cueflow.canonical import artifact_content_hash
from cueflow.config import MAX_USER_KEYWORDS, SCHEMA_VERSION
from cueflow.errors import ContractError

SCOPED_KINDS = frozenset({"correction_transcript", "selection_batch", "selection_result"})
TEXT_REFERENCE_FORMATS = frozenset({"txt", "md", "csv", "json"})


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
        field = "arm" if kind == "correction_transcript" else "batch_id"
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
        for name in (
            "timeline_audio_artifact_id",
            "provider",
            "bucket",
            "object_key",
            "content_hash",
        ):
            _string(payload.get(name), f"media_object.{name}")
        _positive_int(payload.get("byte_length"), "media_object.byte_length")
        if "get_url" in payload:
            raise ContractError("media_object must not persist a presigned GET URL")
    elif kind in {"base_asr", "peer_asr"}:
        _string(payload.get("source_text"), f"{kind}.source_text")
        _timed_units(payload.get("timed_units"), f"{kind}.timed_units")
        _provider_metadata(payload.get("provider_metadata"), f"{kind}.provider_metadata")
    elif kind == "asr_comparison":
        _hunks(payload.get("hunks"))
    elif kind == "correction_transcript":
        if payload.get("arm") not in {"qwen", "kimi"}:
            raise ContractError("Unknown correction arm")
        _string(payload.get("corrected_text"), "corrected_text")
        _provider_metadata(payload.get("provider_metadata"), "provider_metadata")
    elif kind == "selection_batch":
        from cueflow.conflict_selection import validate_batch

        validate_batch(payload)
    elif kind == "selection_result":
        from cueflow.conflict_selection import validate_decisions

        validate_decisions(
            {"decisions": payload.get("decisions")}, _mapping(payload.get("request"), "request")
        )
        _provider_metadata(payload.get("provider_metadata"), "provider_metadata")
    elif kind == "review_resolution":
        _string(payload.get("run_id"), "run_id")
        _string(payload.get("queue_artifact_id"), "queue_artifact_id")
        decisions = payload.get("decisions")
        if not isinstance(decisions, list):
            raise ContractError("review decisions are missing")
        decision_ids: set[str] = set()
        for raw in decisions:
            decision = _mapping(raw, "review decisions[]")
            action = decision.get("action")
            fields = {"review_id", "action", "replacement"} if action == "replace" else {
                "review_id",
                "action",
            }
            if set(decision) != fields:
                raise ContractError("review decision fields do not match the contract")
            review_id = _string(decision.get("review_id"), "review decision.review_id")
            if review_id in decision_ids:
                raise ContractError("review decisions contain a duplicate review_id")
            decision_ids.add(review_id)
            if action not in {"keep", "qwen", "kimi", "peer", "replace"}:
                raise ContractError("invalid review decision action")
            if action == "replace":
                _string(
                    decision.get("replacement"),
                    "review decision.replacement",
                    allow_empty=True,
                )
    elif kind in {"merge_plan", "edit_resolution"}:
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
        from cueflow.edit_resolution import apply_resolved_payload

        rebuilt = apply_resolved_payload(str(payload["base_text"]), resolved)
        if payload.get("corrected_preview") != rebuilt:
            raise ContractError("resolution preview does not rebuild from Base")
        if kind == "merge_plan":
            from cueflow.conflict_selection import build_merge_plan

            variants = _mapping(payload.get("variants"), "variants")
            if set(variants) != {"base", "peer", "qwen", "kimi"}:
                raise ContractError("Merge plan requires four source transcripts")
            texts = {key: _string(value, key, allow_empty=True) for key, value in variants.items()}
            if dict(payload) != build_merge_plan(
                texts["base"], texts["peer"], texts["qwen"], texts["kimi"]
            ):
                raise ContractError("Merge plan provenance does not recompute")
        else:
            _string(payload.get("run_id"), "run_id")
            pending = _non_negative_int(payload.get("pending_selection"), "pending_selection")
            if not isinstance(payload.get("sealed"), bool):
                raise ContractError("resolution.sealed must be boolean")
            if payload["sealed"] and (pending or reviews):
                raise ContractError("sealed resolution has pending work")
    elif kind == "review_queue":
        _string(payload.get("run_id"), "run_id")
        _string(payload.get("resolution_artifact_id"), "resolution_artifact_id")
        items = payload.get("items")
        if not isinstance(items, list):
            raise ContractError("review_queue.items must be an array")
        status = payload.get("status")
        if status not in {"clear", "needs_review"}:
            raise ContractError("invalid review_queue status")
        if (status == "clear") != (not items):
            raise ContractError("review_queue status does not match its items")
        queue_ids: set[str] = set()
        for raw in items:
            item = _mapping(raw, "review_queue.items[]")
            review_id = _string(item.get("review_id"), "review item.review_id")
            if review_id in queue_ids:
                raise ContractError("review_queue contains a duplicate review_id")
            queue_ids.add(review_id)
            start = _non_negative_int(item.get("start"), "review item.start")
            end = _non_negative_int(item.get("end"), "review item.end")
            if end < start:
                raise ContractError("review item has a reversed Base interval")
            original = _string(item.get("original"), "review item.original", allow_empty=True)
            if len(original) != end - start:
                raise ContractError("review item original does not match its Base interval length")
            candidates = _mapping(item.get("candidates"), "review item.candidates")
            for source, text in candidates.items():
                if source not in {"base", "qwen", "kimi", "peer"}:
                    raise ContractError("review item contains an unknown candidate source")
                _string(text, f"review item.candidates.{source}", allow_empty=True)
            if candidates.get("base") != original:
                raise ContractError("review item Base candidate does not match original")
    elif kind == "transcript":
        validate_transcript_payload(payload)
    elif kind == "ata_response":
        _ata_dependencies(payload)
        _string(payload.get("invocation_id"), "invocation_id")
        _string(payload.get("audio_text"), "audio_text", allow_empty=True)
        _provider_metadata(payload.get("provider_metadata"), "provider_metadata")
        _blob(payload.get("response_blob"), "response_blob")
    elif kind == "ata_result":
        _ata_dependencies(payload)
        _string(payload.get("ata_response_artifact_id"), "ata_response_artifact_id")
        validate_ata_result_payload(payload)
    elif kind == "srt_render":
        _string(payload.get("run_id"), "run_id")
        _string(payload.get("ata_result_artifact_id"), "ata_result_artifact_id")
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
        if kind in {"pdf_object", "image_object"}:
            obj = _mapping(item.get("object"), "reference.object")
            for field in ("provider", "bucket", "object_key", "content_hash"):
                _string(obj.get(field), f"reference.object.{field}")
            _positive_int(obj.get("byte_length"), "reference.object.byte_length")
            if "url" in item:
                raise ContractError("Reference must not persist a signed URL")
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
    _string(payload.get("source_text"), "source_text")
    _string(payload.get("base_asr_artifact_id"), "base_asr_artifact_id")
    _string(payload.get("edit_resolution_artifact_id"), "edit_resolution_artifact_id")
    if payload.get("correction_mode") != "dual_fulltext_selection":
        raise ContractError("invalid transcript.correction_mode")


def _ata_dependencies(payload: Mapping[str, Any]) -> None:
    for field in (
        "run_id", "transcript_artifact_id", "media_object_artifact_id", "timeline_audio_artifact_id"
    ):
        _string(payload.get(field), field)


def validate_ata_result_payload(payload: Mapping[str, Any]) -> None:
    utterances = payload.get("utterances")
    if not isinstance(utterances, list):
        raise ContractError("ata_result.utterances must be an array")
    for raw in utterances:
        item = _mapping(raw, "utterances[]")
        _string(item.get("text"), "utterance.text", allow_empty=True)
        for field in ("start_ms", "end_ms"):
            if type(item.get(field)) is not int:
                raise ContractError(f"utterance.{field} must be an integer")
    diagnostics = _mapping(payload.get("diagnostics"), "diagnostics")
    for field in (
        "utterance_count", "empty_text_count", "negative_time_count",
        "reversed_interval_count", "zero_duration_count", "overlap_count"
    ):
        _non_negative_int(diagnostics.get(field), f"diagnostics.{field}")
    comparison = _mapping(diagnostics.get("text_comparison"), "text_comparison")
    if comparison.get("relation") not in {"identical", "differs"}:
        raise ContractError("invalid text comparison relation")
    for field in ("input_length", "output_length"):
        _non_negative_int(comparison.get(field), f"text_comparison.{field}")
    if type(comparison.get("length_delta")) is not int:
        raise ContractError("text_comparison.length_delta must be an integer")


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
