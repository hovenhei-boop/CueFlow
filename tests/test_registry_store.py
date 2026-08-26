from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from cueflow.alignment import build_alignment_payload
from cueflow.artifact_store import ArtifactStore
from cueflow.atomizer import build_transcript_payload
from cueflow.canonical import hash_json
from cueflow.errors import ContractError, IntegrityError, SourceMissingError
from cueflow.glossary import glossary_payload
from cueflow.orchestrator import initialize_project, project_status, set_project_glossary
from cueflow.project import ProjectContext
from cueflow.providers import AlignmentToken
from cueflow.registry import REGISTRY_SCHEMA_VERSION, Registry
from cueflow.schema import ArtifactEnvelope, InputRef, Producer


def producer() -> Producer:
    return Producer(
        component="cueflow.glossary",
        component_version="0.1.0",
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


@pytest.mark.parametrize("version", [0, 1, 2, 99])
def test_incompatible_registry_is_rejected_without_writes(tmp_path: Path, version: int) -> None:
    database = tmp_path / "incompatible.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE sentinel (value TEXT NOT NULL)")
        connection.execute("INSERT INTO sentinel VALUES ('preserve')")
        connection.execute(f"PRAGMA user_version = {version}")
    before = database.read_bytes()
    with pytest.raises(IntegrityError, match="incompatible CueFlow registry schema version"):
        Registry(database)
    assert database.read_bytes() == before


@pytest.mark.parametrize("table", ["projects", "source_assets", "reference_assets"])
def test_current_registry_rejects_unexpected_columns(tmp_path: Path, table: str) -> None:
    context = ProjectContext.create(tmp_path / "project", "Schema")
    database = context.registry.path
    assert set(context.registry.project().keys()) == {"project_id", "display_name", "created_at"}
    context.close()
    with sqlite3.connect(database) as connection:
        connection.execute(f"ALTER TABLE {table} ADD COLUMN unexpected TEXT")
    before = database.read_bytes()
    with pytest.raises(IntegrityError, match=f"registry {table} schema"):
        ProjectContext.open(tmp_path / "project")
    assert database.read_bytes() == before


def test_artifact_file_precedes_pointer_and_pointer_rolls_back(tmp_path: Path) -> None:
    empty_database = tmp_path / "empty.sqlite3"
    sqlite3.connect(empty_database).close()
    empty_registry = Registry(empty_database)
    empty_registry.close()
    with sqlite3.connect(empty_database) as connection:
        assert connection.execute("PRAGMA user_version").fetchone() == (
            REGISTRY_SCHEMA_VERSION,
        )

    old_database = tmp_path / "old.sqlite3"
    with sqlite3.connect(old_database) as connection:
        connection.execute("CREATE TABLE projects (project_id TEXT PRIMARY KEY)")
    with pytest.raises(IntegrityError, match="incompatible CueFlow registry schema version"):
        Registry(old_database)
    with sqlite3.connect(old_database) as connection:
        assert connection.execute("PRAGMA user_version").fetchone() == (0,)
        assert {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        } == {"projects"}

    incomplete_database = tmp_path / "incomplete.sqlite3"
    with sqlite3.connect(incomplete_database) as connection:
        connection.execute("CREATE TABLE projects (project_id TEXT PRIMARY KEY)")
        connection.execute(f"PRAGMA user_version = {REGISTRY_SCHEMA_VERSION}")
    with pytest.raises(IntegrityError, match="schema is incomplete"):
        Registry(incomplete_database)

    project = ProjectContext.create(tmp_path / "project", "Test")
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
    project = ProjectContext.create(tmp_path / "project", "Test")
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
    project = initialize_project(tmp_path / "project", "Test")
    try:
        timeline = ArtifactEnvelope.create(
            artifact_kind="timeline_audio",
            scope_key="global",
            producer=producer(),
            inputs=(),
            payload={
                "duration_ms": 1_000,
                "total_sample_count": 16_000,
                "timeline_origin_sample": 0,
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


def test_auxiliary_asset_does_not_bypass_effective_glossary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = initialize_project(tmp_path / "project", "Test")
    auxiliary = tmp_path / "notes.txt"
    auxiliary.write_text("顾华玺", encoding="utf-8")
    try:
        before = project.current_artifact("effective_glossary")
        registered = project.register_external_asset(auxiliary, asset_kind="auxiliary")
        auxiliary.write_text("新版顾华玺", encoding="utf-8")
        replaced = project.register_external_asset(auxiliary, asset_kind="auxiliary")
        other = tmp_path / "other.txt"
        other.write_text("other", encoding="utf-8")
        other_registered = project.register_external_asset(other, asset_kind="auxiliary")
        after = project.current_artifact("effective_glossary")
        assert registered["asset_kind"] == "auxiliary"
        assert registered["filename"] == "notes.txt"
        assert "content_hash" not in registered and "byte_length" not in registered
        assert replaced["source_asset_id"] == registered["source_asset_id"]
        assert other_registered["source_asset_id"] != registered["source_asset_id"]
        assert after.artifact_id == before.artifact_id
        assert after.payload["terms"] == []

        duplicate_dir = tmp_path / "duplicate"
        duplicate_dir.mkdir()
        duplicate = duplicate_dir / auxiliary.name
        duplicate.write_text("different locator", encoding="utf-8")
        with pytest.raises(ContractError, match="relink is not available"):
            project.register_external_asset(duplicate, asset_kind="auxiliary")
        with pytest.raises(SourceMissingError, match="source_missing"):
            project.register_external_asset(tmp_path / "missing.txt", asset_kind="auxiliary")
        with pytest.raises(SourceMissingError, match="source_missing"):
            project.register_external_asset(duplicate_dir, asset_kind="auxiliary")

        denied = tmp_path / "denied.txt"
        denied.write_text("denied", encoding="utf-8")
        real_open = Path.open

        def denied_open(path: Path, *args: object, **kwargs: object) -> object:
            if path == denied:
                raise PermissionError("denied")
            return real_open(path, *args, **kwargs)

        monkeypatch.setattr(Path, "open", denied_open)
        with pytest.raises(SourceMissingError, match="source_missing"):
            project.register_external_asset(denied, asset_kind="auxiliary")
    finally:
        project.close()


def test_interrupted_run_can_only_reopen_for_targeted_retry(tmp_path: Path) -> None:
    project = initialize_project(tmp_path / "project", "Test")
    try:
        run_id = project.registry.create_run(
            project.project_id,
            {"source_asset_id": "fixture"},
            "sha256:" + "0" * 64,
        )
        project.registry.set_run_status(run_id, "running")
        created_id = project.registry.create_invocation(
            run_id=run_id,
            project_id=project.project_id,
            operation="semantic_transcription",
            logical_operation_key="semantic:chunk_0001:created",
            attempt_number=1,
            provider="fixture",
            model="fixture",
            chunk_id="chunk_0001",
        )
        sending_id = project.registry.create_invocation(
            run_id=run_id,
            project_id=project.project_id,
            operation="semantic_transcription",
            logical_operation_key="semantic:chunk_0001:sending",
            attempt_number=1,
            provider="fixture",
            model="fixture",
            chunk_id="chunk_0001",
        )
        project.registry.set_invocation_status(sending_id, "sending")
        project.close()

        project = ProjectContext.open(tmp_path / "project")
        assert project.registry.run(run_id)["status"] == "running"
        assert project.registry.invocation(created_id)["status"] == "created"
        assert project.registry.invocation(sending_id)["status"] == "sending"
        assert project_status(project)["latest_source_run"]["status"] == "running"
        set_project_glossary(project, ["管理命令"])
        management_asset = tmp_path / "management.txt"
        management_asset.write_text("management", encoding="utf-8")
        project.register_external_asset(management_asset, asset_kind="auxiliary")
        assert project.registry.run(run_id)["status"] == "running"
        assert project.registry.invocation(created_id)["status"] == "created"
        assert project.registry.invocation(sending_id)["status"] == "sending"

        assert project.registry.recover_running_runs() == [run_id]
        assert project.registry.run(run_id)["status"] == "interrupted"
        assert project.registry.invocation(created_id)["status"] == "definitely_not_sent"
        assert project.registry.invocation(sending_id)["status"] == "delivery_ambiguous"
        assert project.registry.sent_semantic_attempt_count(run_id, "chunk_0001", 0) == 1
        project.registry.reopen_run_for_retry(run_id)
        assert project.registry.run(run_id)["status"] == "running"
        project.registry.set_run_status(run_id, "succeeded")
        with pytest.raises(ContractError, match="failed or interrupted"):
            project.registry.reopen_run_for_retry(run_id)
        with pytest.raises(ContractError, match="invalid run status"):
            project.registry.set_run_status(run_id, "cancelled")
    finally:
        project.close()
