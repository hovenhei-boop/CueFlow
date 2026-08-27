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
from cueflow.registry import (
    LEGACY_REGISTRY_SCHEMA_VERSION,
    REGISTRY_SCHEMA_VERSION,
    Registry,
    migrate_registry,
)
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


def _v4_registry_fixture(project_root: Path) -> Path:
    context = ProjectContext.create(project_root, "Legacy Lexicon")
    database = context.registry.path
    context.close()
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA foreign_keys = OFF")
        connection.execute("PRAGMA ignore_check_constraints = ON")
        connection.execute("DROP TABLE lexicon_blacklist")
        connection.execute(
            """
            CREATE TABLE lexicon_blacklist (
                blacklist_id TEXT PRIMARY KEY,
                normalization_version TEXT NOT NULL,
                normalized_surface_form TEXT NOT NULL COLLATE BINARY,
                surface_form TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE (normalization_version, normalized_surface_form)
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE lexicon_trash (
                trash_id TEXT PRIMARY KEY,
                object_kind TEXT NOT NULL CHECK (object_kind IN ('candidate','entry')),
                object_id TEXT NOT NULL,
                normalization_version TEXT NOT NULL,
                normalized_surface_form TEXT NOT NULL COLLATE BINARY,
                restore_payload_json TEXT NOT NULL,
                deleted_at TEXT NOT NULL,
                expires_at TEXT,
                status TEXT NOT NULL CHECK (status IN ('active','restored','expired')),
                restored_at TEXT
            )
            """
        )
        connection.execute(
            "CREATE INDEX lexicon_trash_active_term "
            "ON lexicon_trash(normalization_version, normalized_surface_form, status)"
        )
        connection.execute(
            """
            CREATE TABLE lexicon_settings (
                singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                trash_retention_days INTEGER
                    CHECK (trash_retention_days IS NULL
                           OR trash_retention_days IN (15,30,60,120)),
                updated_at TEXT NOT NULL
            )
            """
        )
        now = "2026-08-28T00:00:00Z"
        candidates = (
            ("cand_active", "Active", "Active", "blacklisted"),
            ("cand_temp", "CandidateTemp", "CandidateTemp", "rejected"),
            ("cand_latest", "CandidateLatest", "CandidateLatest", "rejected"),
            ("cand_expired", "CandidateExpired", "CandidateExpired", "rejected"),
        )
        connection.executemany(
            """
            INSERT INTO term_candidates
            (candidate_id, normalization_version, normalized_surface_form,
             display_term, display_category, proper_noun_subtype, status,
             revision, created_at, updated_at)
            VALUES (?, '0.1.0', ?, ?, 'noun_or_term', NULL, ?, 1, ?, ?)
            """,
            [(*row, now, now) for row in candidates],
        )
        connection.executemany(
            """
            INSERT INTO project_lexicon_entries
            (entry_id, term, normalization_version, normalized_surface_form,
             category, proper_noun_subtype, source_candidate_id, enabled,
             status, revision, created_at, updated_at)
            VALUES (?, ?, '0.1.0', ?, 'noun_or_term', NULL, ?, 1, ?, 1, ?, ?)
            """,
            (
                ("lex_active", "Active", "Active", "cand_active", "active", now, now),
                ("lex_never", "EntryNever", "EntryNever", None, "deleted", now, now),
            ),
        )
        connection.execute(
            "INSERT INTO lexicon_blacklist VALUES "
            "('black_active', '0.1.0', 'Active', 'Active', ?)",
            (now,),
        )
        connection.executemany(
            """
            INSERT INTO lexicon_trash
            (trash_id, object_kind, object_id, normalization_version,
             normalized_surface_form, restore_payload_json, deleted_at,
             expires_at, status, restored_at)
            VALUES (?, ?, ?, '0.1.0', ?, '{}', ?, ?, 'active', NULL)
            """,
            (
                (
                    "trash_temp",
                    "candidate",
                    "cand_temp",
                    "CandidateTemp",
                    now,
                    "2099-01-01T00:00:00Z",
                ),
                ("trash_never", "entry", "lex_never", "EntryNever", now, None),
                (
                    "trash_temp_never",
                    "candidate",
                    "cand_temp",
                    "CandidateTemp",
                    now,
                    None,
                ),
                (
                    "trash_latest_early",
                    "candidate",
                    "cand_latest",
                    "CandidateLatest",
                    now,
                    "2040-01-01T00:00:00Z",
                ),
                (
                    "trash_latest_late",
                    "candidate",
                    "cand_latest",
                    "CandidateLatest",
                    now,
                    "2050-01-01T00:00:00Z",
                ),
                (
                    "trash_expired",
                    "candidate",
                    "cand_expired",
                    "CandidateExpired",
                    now,
                    "2000-01-01T00:00:00Z",
                ),
            ),
        )
        connection.execute("INSERT INTO lexicon_settings VALUES (1, 30, ?)", (now,))
        connection.execute(f"PRAGMA user_version = {LEGACY_REGISTRY_SCHEMA_VERSION}")
    return database


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


def test_explicit_v4_to_v5_migration_preserves_suppression_and_exclusivity(
    tmp_path: Path,
) -> None:
    database = _v4_registry_fixture(tmp_path / "project")
    with pytest.raises(IntegrityError, match="cueflow migrate"):
        ProjectContext.open(tmp_path / "project")

    result = migrate_registry(database)
    assert result == {
        "status": "migrated",
        "from_version": 4,
        "to_version": 5,
        "blacklist_rules": 3,
        "glossary_conflicts_resolved": 1,
    }
    assert migrate_registry(database)["status"] == "already_current"

    context = ProjectContext.open(tmp_path / "project")
    try:
        connection = context.registry._connection
        version = connection.execute("PRAGMA user_version").fetchone()
        assert version is not None and version[0] == REGISTRY_SCHEMA_VERSION
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert "lexicon_trash" not in tables
        assert "lexicon_settings" not in tables
        rules = connection.execute(
            "SELECT normalized_surface_form, kind, expires_at "
            "FROM lexicon_blacklist ORDER BY normalized_surface_form"
        ).fetchall()
        assert [tuple(row) for row in rules] == [
            ("CandidateLatest", "temporary", "2050-01-01T00:00:00Z"),
            ("CandidateTemp", "permanent", None),
            ("EntryNever", "permanent", None),
        ]
        statuses = {
            row["normalized_surface_form"]: row["status"]
            for row in connection.execute("SELECT * FROM term_candidates").fetchall()
        }
        assert statuses == {
            "Active": "accepted",
            "CandidateExpired": "dismissed",
            "CandidateLatest": "blacklisted",
            "CandidateTemp": "blacklisted",
        }
        entries = {
            row["normalized_surface_form"]: row["status"]
            for row in connection.execute("SELECT * FROM project_lexicon_entries").fetchall()
        }
        assert entries == {"Active": "active", "EntryNever": "removed"}
        audit = connection.execute(
            "SELECT action FROM candidate_decisions "
            "WHERE action='migration_suppression_removed_for_active_entry'"
        ).fetchall()
        assert len(audit) == 1
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        context.close()


def test_v4_to_v5_migration_rolls_back_on_foreign_key_failure(tmp_path: Path) -> None:
    database = _v4_registry_fixture(tmp_path / "project")
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA foreign_keys = OFF")
        connection.execute(
            """
            INSERT INTO term_occurrences
            (occurrence_id, candidate_id, evidence_artifact_id, reference_role,
             raw_surface_form, suggested_surface_form, proposed_category,
             proper_noun_subtype, risk_tags_json, field_path_json, start_offset,
             end_offset, context_before, context_after, coordinates_json, created_at)
            VALUES ('occ_bad', 'missing', 'evidence', 'document_text', 'bad', NULL,
                    'noun_or_term', NULL, '[]', '["content"]', 0, 3, '', '', NULL,
                    '2026-08-28T00:00:00Z')
            """
        )

    with pytest.raises(IntegrityError, match="foreign-key validation"):
        migrate_registry(database)

    with sqlite3.connect(database) as connection:
        version = connection.execute("PRAGMA user_version").fetchone()
        assert version == (LEGACY_REGISTRY_SCHEMA_VERSION,)
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert "lexicon_trash" in tables
        assert "lexicon_settings" in tables
        assert not any(name.endswith("_v5") for name in tables)


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
        assert project.registry.run(run_id)["kind"] == "source"
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
