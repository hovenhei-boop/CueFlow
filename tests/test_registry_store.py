from __future__ import annotations

from pathlib import Path

import pytest

from cueflow.alignment import build_alignment_payload
from cueflow.artifact_store import ArtifactStore
from cueflow.atomizer import build_transcript_payload
from cueflow.canonical import hash_json
from cueflow.glossary import glossary_payload
from cueflow.orchestrator import initialize_project, set_project_glossary
from cueflow.project import ProjectContext
from cueflow.providers import AlignmentToken
from cueflow.schema import ArtifactEnvelope, InputRef, Producer


def producer() -> Producer:
    return Producer(
        component="cueflow.glossary",
        component_version="0.1.0",
        processing_profile="LOCAL_PROFILE",
        provider=None,
        model=None,
        config_hash=hash_json({"version": "0.1.0"}),
    )


def publish_glossary(project: ProjectContext, terms: list[str]) -> ArtifactEnvelope:
    envelope = ArtifactEnvelope.create(
        artifact_kind="project_glossary",
        scope_key="global",
        producer=producer(),
        inputs=(),
        payload=glossary_payload(terms),
    )
    return project.publisher.publish(envelope)


def test_artifact_file_precedes_pointer_and_pointer_rolls_back(tmp_path: Path) -> None:
    project = ProjectContext.create(tmp_path / "project", "Test", "LOCAL_PROFILE")
    try:
        first = publish_glossary(project, ["顾华玺"])
        pointer = project.registry.current_pointer(project.project_id, "project_glossary", "global")
        assert pointer is not None and pointer["artifact_id"] == first.artifact_id

        with pytest.raises(RuntimeError):
            with project.registry.transaction() as connection:
                connection.execute(
                    "UPDATE current_pointers SET is_stale=1 WHERE project_id=?",
                    (project.project_id,),
                )
                raise RuntimeError("force rollback")
        pointer = project.registry.current_pointer(project.project_id, "project_glossary", "global")
        assert pointer is not None and pointer["is_stale"] == 0
    finally:
        project.close()


def test_unregistered_envelope_is_reported_as_orphan(tmp_path: Path) -> None:
    project = ProjectContext.create(tmp_path / "project", "Test", "LOCAL_PROFILE")
    try:
        envelope = ArtifactEnvelope.create(
            artifact_kind="system_glossary",
            scope_key="global",
            producer=producer(),
            inputs=(),
            payload=glossary_payload([]),
        )
        orphan_path = project.store.write_envelope(envelope)
        assert orphan_path in project.registry.orphan_candidates(project.store.artifacts_root)
    finally:
        project.close()


def test_blob_is_content_addressed_and_verified(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path / "project")
    source = tmp_path / "payload.bin"
    source.write_bytes(b"cueflow")
    content_hash, length, path = store.publish_blob(source)
    assert length == 7
    assert path == store.blob_path(content_hash)
    store.verify_blob(path, content_hash, length)


def test_chunk_scope_switch_and_stale_routes_are_independent(tmp_path: Path) -> None:
    project = initialize_project(tmp_path / "project", "Test", "LOCAL_PROFILE")
    try:
        timeline = ArtifactEnvelope.create(
            artifact_kind="timeline_audio",
            scope_key="global",
            producer=producer(),
            inputs=(),
            payload={
                "duration_ms": 1_000,
                "sample_rate_hz": 16_000,
                "channels": 1,
                "sample_format": "s16le",
            },
        )
        project.publisher.publish(timeline)
        transcripts: list[ArtifactEnvelope] = []
        alignments: list[ArtifactEnvelope] = []
        for index, text in enumerate(("甲乙", "丙丁"), start=1):
            chunk_id = f"chunk_{index:04d}"
            media_chunk = ArtifactEnvelope.create(
                artifact_kind="media_chunk",
                scope_key=chunk_id,
                producer=producer(),
                inputs=[InputRef(role="timeline_audio", artifact_id=timeline.artifact_id)],
                payload={
                    "chunk_id": chunk_id,
                    "global_start_ms": (index - 1) * 500,
                    "global_end_ms": index * 500,
                    "timeline_audio_artifact_id": timeline.artifact_id,
                },
            )
            project.publisher.publish(media_chunk)
            transcript_payload = build_transcript_payload(
                chunk_id=chunk_id, source_text=text, language="Chinese"
            )
            transcript = ArtifactEnvelope.create(
                artifact_kind="transcript",
                scope_key=chunk_id,
                producer=producer(),
                inputs=[InputRef(role="media_chunk", artifact_id=media_chunk.artifact_id)],
                payload=transcript_payload,
            )
            project.publisher.publish(transcript)
            tokens = [
                AlignmentToken(str(atom["text"]), offset * 100, (offset + 1) * 100)
                for offset, atom in enumerate(transcript_payload["atoms"])
            ]
            alignment_payload = build_alignment_payload(
                media_chunk_artifact_id=media_chunk.artifact_id,
                media_chunk=media_chunk.payload,
                transcript_artifact_id=transcript.artifact_id,
                transcript=transcript.payload,
                tokens=tokens,
            )
            alignment = ArtifactEnvelope.create(
                artifact_kind="alignment",
                scope_key=chunk_id,
                producer=producer(),
                inputs=[
                    InputRef(role="media_chunk", artifact_id=media_chunk.artifact_id),
                    InputRef(role="transcript", artifact_id=transcript.artifact_id),
                ],
                payload=alignment_payload,
            )
            project.publisher.publish(alignment)
            transcripts.append(transcript)
            alignments.append(alignment)

        new_payload = build_transcript_payload(
            chunk_id="chunk_0001", source_text="甲丙", language="Chinese"
        )
        new_transcript = ArtifactEnvelope.create(
            artifact_kind="transcript",
            scope_key="chunk_0001",
            producer=producer(),
            inputs=list(transcripts[0].inputs),
            payload=new_payload,
        )
        project.publisher.publish(
            new_transcript,
            stale_targets=[("alignment", "chunk_0001"), ("subtitle", None)],
        )
        first_alignment = project.registry.current_pointer(
            project.project_id, "alignment", "chunk_0001"
        )
        second_alignment = project.registry.current_pointer(
            project.project_id, "alignment", "chunk_0002"
        )
        assert first_alignment is not None and first_alignment["is_stale"] == 1
        assert second_alignment is not None and second_alignment["is_stale"] == 0
        assert project.registry.current_pointer(
            project.project_id, "transcript", "chunk_0002"
        )["artifact_id"] == transcripts[1].artifact_id

        set_project_glossary(project, ["顾华玺"])
        assert all(
            row["is_stale"] == 1
            for kind in ("transcript", "alignment")
            for row in project.registry.current_pointers(project.project_id, kind)
        )
    finally:
        project.close()


def test_auxiliary_asset_does_not_bypass_effective_glossary(tmp_path: Path) -> None:
    project = initialize_project(tmp_path / "project", "Test", "LOCAL_PROFILE")
    auxiliary = tmp_path / "notes.txt"
    auxiliary.write_text("顾华玺", encoding="utf-8")
    try:
        before = project.current_artifact("effective_glossary")
        registered = project.register_external_asset(auxiliary, asset_kind="auxiliary")
        after = project.current_artifact("effective_glossary")
        assert registered["asset_kind"] == "auxiliary"
        assert after.artifact_id == before.artifact_id
        assert after.payload["terms"] == []
    finally:
        project.close()
