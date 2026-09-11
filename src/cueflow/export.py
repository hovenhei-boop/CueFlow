from __future__ import annotations

import os
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from cueflow.artifact_versions import artifact_producer_version
from cueflow.canonical import hash_json
from cueflow.errors import ContractError, ExportBlockedError, SrtSerializationError
from cueflow.project import RunContext
from cueflow.run_runtime import _get
from cueflow.schema import ArtifactEnvelope, InputRef, Producer


def render_srt(utterances: Sequence[Mapping[str, Any]]) -> str:
    blocks: list[str] = []
    for index, item in enumerate(utterances, start=1):
        text, start, end = item.get("text"), item.get("start_ms"), item.get("end_ms")
        if not isinstance(text, str) or type(start) is not int or type(end) is not int:
            raise SrtSerializationError(f"Utterance {index} requires text and integer milliseconds")
        if start < 0 or end < 0 or start > end:
            raise SrtSerializationError(f"Utterance {index} has negative or reversed SRT times")
        blocks.append(f"{index}\n{_srt_time(start)} --> {_srt_time(end)}\n{text}")
    return "\n\n".join(blocks) + ("\n" if blocks else "")


def publish_srt(
    context: RunContext,
    *,
    run_id: str,
    timeline_audio: ArtifactEnvelope,
    transcript: ArtifactEnvelope,
    ata_response: ArtifactEnvelope,
    ata_result: ArtifactEnvelope,
) -> tuple[ArtifactEnvelope, Path]:
    validate_export_gate(
        context, run_id=run_id, timeline_audio=timeline_audio, transcript=transcript,
        ata_response=ata_response, ata_result=ata_result,
    )
    text = render_srt(ata_result.payload["utterances"])
    envelope = ArtifactEnvelope.create(
        artifact_kind="srt_render",
        scope_key="global",
        producer=Producer(
            component="srt_render",
            component_version=artifact_producer_version("srt_render"),
            provider=None,
            model=None,
            config_hash=hash_json({"encoding": "utf-8", "serializer": "utterances-v1"}),
        ),
        inputs=[InputRef(role="ata_result", artifact_id=ata_result.artifact_id)],
        payload={
            "run_id": run_id,
            "ata_result_artifact_id": ata_result.artifact_id,
            "encoding": "utf-8",
            "byte_length": len(text.encode("utf-8")),
            "text": text,
        },
    )
    context.publisher.publish(envelope)
    directory = context.root / "attempts" / str(context.registry.round_number(run_id))
    destination = directory / "final.srt"
    _atomic_text_projection(text, destination)
    return envelope, destination


def validate_export_gate(
    context: RunContext,
    *,
    run_id: str,
    timeline_audio: ArtifactEnvelope,
    transcript: ArtifactEnvelope,
    ata_response: ArtifactEnvelope,
    ata_result: ArtifactEnvelope,
) -> None:
    """Validate ownership and provenance, never subtitle quality."""
    if run_id != context.run_id:
        raise ExportBlockedError("Run belongs to another project")
    for envelope in (timeline_audio, transcript, ata_response, ata_result):
        _require_current(context, envelope)
        checkpoint = _get(context, run_id, envelope.artifact_kind, envelope.scope_key)
        if checkpoint is None or checkpoint.artifact_id != envelope.artifact_id:
            raise ExportBlockedError("Artifact is not the requested Run's checkpoint")
    resolution = context.artifact(str(transcript.payload["edit_resolution_artifact_id"]))
    _require_current(context, resolution)
    if (
        resolution.payload.get("run_id") != run_id
        or not resolution.payload.get("sealed")
        or resolution.payload.get("pending_selection")
        or resolution.payload.get("review_items")
        or resolution.payload.get("corrected_preview") != transcript.payload["source_text"]
    ):
        raise ExportBlockedError("Transcript requires this Run's sealed, fully resolved source")
    media_object = context.artifact(str(ata_response.payload["media_object_artifact_id"]))
    _require_current(context, media_object)
    checkpoint = _get(context, run_id, "media_object")
    if checkpoint is None or checkpoint.artifact_id != media_object.artifact_id:
        raise ExportBlockedError("ATA media object is not the requested Run's checkpoint")
    audio_blob = timeline_audio.payload["audio_blob"]
    if (
        media_object.payload.get("timeline_audio_artifact_id") != timeline_audio.artifact_id
        or media_object.payload.get("content_hash") != audio_blob["content_hash"]
        or media_object.payload.get("byte_length") != audio_blob["byte_length"]
    ):
        raise ExportBlockedError("ATA media object references a different TimelineAudio")
    _require_inputs(media_object, [("timeline_audio", timeline_audio.artifact_id)])
    for envelope in (ata_response, ata_result):
        if (
            envelope.payload["run_id"] != run_id
            or envelope.payload["transcript_artifact_id"] != transcript.artifact_id
            or envelope.payload["timeline_audio_artifact_id"] != timeline_audio.artifact_id
            or envelope.payload["media_object_artifact_id"] != media_object.artifact_id
        ):
            raise ExportBlockedError("ATA provenance differs from this Run's frozen inputs")
    if ata_result.payload["ata_response_artifact_id"] != ata_response.artifact_id:
        raise ExportBlockedError("ATA result references a different raw response")
    if ata_response.payload["audio_text"] != transcript.payload["source_text"]:
        raise ExportBlockedError("ATA request text differs from the sealed Transcript")
    _require_inputs(ata_response, [
        ("media_object", media_object.artifact_id),
        ("timeline_audio", timeline_audio.artifact_id),
        ("transcript", transcript.artifact_id),
    ])
    _require_inputs(ata_result, [
        ("ata_response", ata_response.artifact_id),
        ("transcript", transcript.artifact_id),
        ("timeline_audio", timeline_audio.artifact_id),
    ])
    invocation = context.registry.invocation(str(ata_response.payload["invocation_id"]))
    invocation_inputs = [
        (row["role"], row["input_artifact_id"])
        for row in context.registry.invocation_inputs(str(invocation["invocation_id"]))
    ]
    if (
        invocation["run_id"] != run_id
        or invocation["operation"] != "ata"
        or invocation["status"] != "succeeded"
        or invocation["artifact_id"] != ata_response.artifact_id
        or invocation["response_id"] != ata_response.payload["provider_metadata"]["response_id"]
        or invocation_inputs != [
            ("media_object", media_object.artifact_id), ("transcript", transcript.artifact_id)
        ]
    ):
        raise ExportBlockedError("ATA raw response requires its successful Run invocation")
    blob = ata_response.payload["response_blob"]
    context.store.verify_blob(
        context.store.blob_path(blob["content_hash"]), blob["content_hash"], blob["byte_length"]
    )


def _require_inputs(envelope: ArtifactEnvelope, expected: list[tuple[str, str]]) -> None:
    if list(envelope.inputs) != [
        InputRef(role=role, artifact_id=identity) for role, identity in expected
    ]:
        raise ExportBlockedError("ATA dependency edges differ from its provenance")


def _require_current(context: RunContext, envelope: ArtifactEnvelope) -> None:
    pointer = context.registry.current_pointer(
        context.run_id, envelope.artifact_kind, envelope.scope_key
    )
    if (
        pointer is None
        or pointer["artifact_id"] != envelope.artifact_id
        or bool(pointer["is_stale"])
    ):
        raise ExportBlockedError(
            f"Artifact is not current and non-stale: {envelope.artifact_kind}/{envelope.scope_key}"
        )
    # created_at is not semantic identity: identical re-publications may have another timestamp.
    try:
        envelope.validate()
    except ContractError as exc:
        raise ExportBlockedError("Supplied artifact violates its schema/hash contract") from exc
    if context.artifact(envelope.artifact_id).content_hash != envelope.content_hash:
        raise ExportBlockedError("Supplied artifact differs from its persisted envelope")


def _atomic_text_projection(text: str, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, raw_path = tempfile.mkstemp(
        prefix="subtitles-", suffix=".srt.tmp", dir=destination.parent
    )
    temp_path = Path(raw_path)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as output:
            output.write(text)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temp_path, destination)
    except BaseException:
        temp_path.unlink(missing_ok=True)
        raise


def _srt_time(milliseconds: int) -> str:
    hours, remainder = divmod(milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    seconds, millis = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{millis:03d}"
