from __future__ import annotations

import os
import tempfile
from collections.abc import Mapping
from pathlib import Path

from cueflow.canonical import hash_json
from cueflow.config import COMPONENT_VERSION
from cueflow.errors import ExportBlockedError
from cueflow.project import ProjectContext
from cueflow.schema import ArtifactEnvelope, InputRef, Producer


def render_srt(subtitle: Mapping[str, object]) -> str:
    blocks: list[str] = []
    cues = subtitle.get("cues", [])
    if not isinstance(cues, list):
        raise ExportBlockedError("Subtitle cues are invalid")
    for index, cue in enumerate(cues, start=1):
        if not isinstance(cue, Mapping):
            raise ExportBlockedError("Subtitle contains an invalid Cue")
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
    timeline_audio: ArtifactEnvelope,
    transcript: ArtifactEnvelope,
    alignment: ArtifactEnvelope,
    subtitle: ArtifactEnvelope,
    qa: ArtifactEnvelope,
) -> tuple[ArtifactEnvelope, Path]:
    validate_export_gate(
        context,
        timeline_audio=timeline_audio,
        transcript=transcript,
        alignment=alignment,
        subtitle=subtitle,
        qa=qa,
    )
    text = render_srt(subtitle.payload)
    envelope = ArtifactEnvelope.create(
        artifact_kind="srt_render",
        scope_key="global",
        producer=Producer(
            component="srt_render",
            component_version=COMPONENT_VERSION,
            provider=None,
            model=None,
            config_hash=hash_json({"encoding": "utf-8", "format": "srt"}),
        ),
        inputs=[
            InputRef(role="subtitle", artifact_id=subtitle.artifact_id),
            InputRef(role="qa", artifact_id=qa.artifact_id),
        ],
        payload={
            "subtitle_artifact_id": subtitle.artifact_id,
            "qa_artifact_id": qa.artifact_id,
            "encoding": "utf-8",
            "byte_length": len(text.encode("utf-8")),
            "text": text,
        },
    )
    context.publisher.publish(envelope)
    destination = context.root / "output" / "subtitles.srt"
    _atomic_text_projection(text, destination)
    return envelope, destination


def validate_export_gate(
    context: ProjectContext,
    *,
    timeline_audio: ArtifactEnvelope,
    transcript: ArtifactEnvelope,
    alignment: ArtifactEnvelope,
    subtitle: ArtifactEnvelope,
    qa: ArtifactEnvelope,
) -> None:
    for envelope in (timeline_audio, transcript, alignment, subtitle, qa):
        _require_current(context, envelope)
    resolution = context.artifact(str(transcript.payload["edit_resolution_artifact_id"]))
    _require_current(context, resolution)
    if (
        not resolution.payload.get("sealed")
        or resolution.payload.get("pending_acoustic")
        or resolution.payload.get("review_items")
        or resolution.payload.get("corrected_preview") != transcript.payload["source_text"]
    ):
        raise ExportBlockedError("Transcript requires its sealed, fully resolved source")
    if qa.payload.get("result") == "blocked":
        raise ExportBlockedError("QA contains unresolved structural blocking errors")
    if alignment.payload.get("timeline_audio_artifact_id") != timeline_audio.artifact_id:
        raise ExportBlockedError("Alignment references a non-current TimelineAudio")
    if alignment.payload.get("transcript_artifact_id") != transcript.artifact_id:
        raise ExportBlockedError("Alignment references a non-current Transcript")
    if subtitle.payload.get("transcript_artifact_id") != transcript.artifact_id:
        raise ExportBlockedError("Subtitle references a non-current Transcript")
    if subtitle.payload.get("alignment_artifact_id") != alignment.artifact_id:
        raise ExportBlockedError("Subtitle references a non-current Alignment")
    qa_inputs = [item.artifact_id for item in qa.inputs if item.artifact_id is not None]
    if qa.payload.get("subject_artifact_ids") != qa_inputs:
        raise ExportBlockedError("QA subjects differ from exact dependency edges")


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
