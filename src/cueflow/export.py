from __future__ import annotations

import os
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import asdict
from pathlib import Path
from typing import Any

from cueflow.canonical import hash_json
from cueflow.config import COMPONENT_VERSION, SegmenterConfig
from cueflow.errors import ExportBlockedError
from cueflow.project import ProjectContext
from cueflow.schema import ArtifactEnvelope, InputRef, Producer


def render_srt(subtitle: Mapping[str, Any]) -> str:
    blocks: list[str] = []
    for index, cue in enumerate(subtitle.get("cues", []), start=1):
        text = str(cue.get("text", ""))
        if not text:
            raise ExportBlockedError("Subtitle contains an empty Cue")
        blocks.append(
            f"{index}\n{_srt_time(int(cue['global_start_ms']))} --> "
            f"{_srt_time(int(cue['global_end_ms']))}\n{text}"
        )
    return "\n\n".join(blocks) + ("\n" if blocks else "")


def publish_srt(
    context: ProjectContext,
    *,
    chunk_plan: ArtifactEnvelope,
    transcripts: Sequence[ArtifactEnvelope],
    alignments: Sequence[ArtifactEnvelope],
    subtitle: ArtifactEnvelope,
    qa: ArtifactEnvelope,
) -> tuple[ArtifactEnvelope, Path]:
    validate_export_gate(
        context,
        chunk_plan=chunk_plan,
        transcripts=transcripts,
        alignments=alignments,
        subtitle=subtitle,
        qa=qa,
    )
    text = render_srt(subtitle.payload)
    config = SegmenterConfig()
    producer = Producer(
        component="srt_render",
        component_version=COMPONENT_VERSION,
        provider=None,
        model=None,
        config_hash=hash_json(asdict(config)),
    )
    payload = {
        "subtitle_artifact_id": subtitle.artifact_id,
        "qa_artifact_id": qa.artifact_id,
        "encoding": "utf-8",
        "byte_length": len(text.encode("utf-8")),
        "text": text,
    }
    envelope = ArtifactEnvelope.create(
        artifact_kind="srt_render",
        scope_key="global",
        producer=producer,
        inputs=[
            InputRef(role="subtitle", artifact_id=subtitle.artifact_id),
            InputRef(role="qa", artifact_id=qa.artifact_id),
        ],
        payload=payload,
    )
    context.publisher.publish(envelope)
    destination = context.root / "output" / "subtitles.srt"
    _atomic_text_projection(text, destination)
    return envelope, destination


def validate_export_gate(
    context: ProjectContext,
    *,
    chunk_plan: ArtifactEnvelope,
    transcripts: Sequence[ArtifactEnvelope],
    alignments: Sequence[ArtifactEnvelope],
    subtitle: ArtifactEnvelope,
    qa: ArtifactEnvelope,
) -> None:
    _require_current(context, chunk_plan)
    _require_current(context, subtitle)
    _require_current(context, qa)
    if qa.payload.get("result") == "blocked":
        raise ExportBlockedError("QA contains unresolved structural blocking errors")
    chunks = chunk_plan.payload.get("chunks")
    if not isinstance(chunks, list):
        raise ExportBlockedError("current ChunkPlan has no chunks")
    transcript_by_chunk = {str(item.payload.get("chunk_id")): item for item in transcripts}
    alignment_by_chunk = {str(item.payload.get("chunk_id")): item for item in alignments}
    expected_ids = {str(item["chunk_id"]) for item in chunks}
    if set(transcript_by_chunk) != expected_ids or set(alignment_by_chunk) != expected_ids:
        raise ExportBlockedError("current ChunkPlan is not covered by current Chunk Artifacts")
    for chunk_id in expected_ids:
        transcript = transcript_by_chunk[chunk_id]
        alignment = alignment_by_chunk[chunk_id]
        _require_current(context, transcript)
        _require_current(context, alignment)
        media_pointer = context.registry.current_pointer(
            context.project_id, "media_chunk", chunk_id
        )
        if media_pointer is None or bool(media_pointer["is_stale"]):
            raise ExportBlockedError(f"MediaChunk is missing or stale: {chunk_id}")
        if alignment.payload.get("transcript_artifact_id") != transcript.artifact_id:
            raise ExportBlockedError(f"Alignment references a non-current Transcript: {chunk_id}")
        if alignment.payload.get("media_chunk_artifact_id") != media_pointer["artifact_id"]:
            raise ExportBlockedError(f"Alignment references a non-current MediaChunk: {chunk_id}")
    expected_subtitle_inputs = {item.artifact_id for item in [*transcripts, *alignments]}
    actual_subtitle_inputs = {
        item.artifact_id
        for item in subtitle.inputs
        if item.role in {"transcript", "alignment"} and item.artifact_id is not None
    }
    if actual_subtitle_inputs != expected_subtitle_inputs:
        raise ExportBlockedError("Subtitle dependency identity is not current")
    if not any(item.artifact_id == subtitle.artifact_id for item in qa.inputs):
        raise ExportBlockedError("QA does not depend on the current Subtitle")
    qa_input_ids = [item.artifact_id for item in qa.inputs if item.artifact_id is not None]
    if qa.payload.get("subject_artifact_ids") != qa_input_ids:
        raise ExportBlockedError("QA subject identities differ from its dependency edges")


def _require_current(context: ProjectContext, envelope: ArtifactEnvelope) -> None:
    pointer = context.registry.current_pointer(
        context.project_id, envelope.artifact_kind, envelope.scope_key
    )
    if (
        pointer is None
        or pointer["artifact_id"] != envelope.artifact_id
        or bool(pointer["is_stale"])
    ):
        raise ExportBlockedError(
            f"Artifact is not current and non-stale: {envelope.artifact_kind}/{envelope.scope_key}"
        )


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
