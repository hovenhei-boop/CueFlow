from __future__ import annotations

import json
import sqlite3
import uuid
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any, cast

from cueflow.errors import ContractError, IntegrityError
from cueflow.schema import ArtifactEnvelope, utc_now

DDL = """
PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;

CREATE TABLE IF NOT EXISTS projects (
    project_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    created_at TEXT NOT NULL,
    processing_profile TEXT NOT NULL CHECK (processing_profile IN ('LOCAL_PROFILE','CLOUD_PROFILE'))
);

CREATE TABLE IF NOT EXISTS source_assets (
    project_id TEXT NOT NULL,
    source_asset_id TEXT NOT NULL,
    asset_kind TEXT NOT NULL CHECK (asset_kind IN ('media','auxiliary')),
    media_kind TEXT,
    format TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    byte_length INTEGER NOT NULL CHECK (byte_length >= 0),
    storage_mode TEXT NOT NULL,
    storage_locator TEXT NOT NULL,
    registered_at TEXT NOT NULL,
    PRIMARY KEY (project_id, source_asset_id),
    FOREIGN KEY (project_id) REFERENCES projects(project_id)
);

CREATE TABLE IF NOT EXISTS artifacts (
    project_id TEXT NOT NULL,
    artifact_id TEXT NOT NULL,
    artifact_kind TEXT NOT NULL,
    scope_key TEXT NOT NULL,
    schema_version TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    storage_locator TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (project_id, artifact_id),
    UNIQUE (project_id, content_hash),
    FOREIGN KEY (project_id) REFERENCES projects(project_id)
);

CREATE TABLE IF NOT EXISTS artifact_dependencies (
    project_id TEXT NOT NULL,
    artifact_id TEXT NOT NULL,
    ordinal INTEGER NOT NULL,
    role TEXT NOT NULL,
    input_artifact_id TEXT,
    input_source_asset_id TEXT,
    coordinate_range_json TEXT,
    PRIMARY KEY (project_id, artifact_id, ordinal),
    CHECK ((input_artifact_id IS NULL) <> (input_source_asset_id IS NULL)),
    FOREIGN KEY (project_id, artifact_id) REFERENCES artifacts(project_id, artifact_id),
    FOREIGN KEY (project_id, input_artifact_id) REFERENCES artifacts(project_id, artifact_id),
    FOREIGN KEY (project_id, input_source_asset_id)
        REFERENCES source_assets(project_id, source_asset_id)
);

CREATE TABLE IF NOT EXISTS current_pointers (
    project_id TEXT NOT NULL,
    artifact_kind TEXT NOT NULL,
    scope_key TEXT NOT NULL,
    artifact_id TEXT NOT NULL,
    is_stale INTEGER NOT NULL DEFAULT 0 CHECK (is_stale IN (0,1)),
    updated_at TEXT NOT NULL,
    PRIMARY KEY (project_id, artifact_kind, scope_key),
    FOREIGN KEY (project_id, artifact_id) REFERENCES artifacts(project_id, artifact_id)
);

CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    status TEXT NOT NULL,
    input_identity_json TEXT NOT NULL,
    config_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    error_message TEXT,
    FOREIGN KEY (project_id) REFERENCES projects(project_id)
);

CREATE TABLE IF NOT EXISTS invocations (
    invocation_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    chunk_id TEXT,
    operation TEXT NOT NULL,
    logical_operation_key TEXT NOT NULL,
    attempt_number INTEGER NOT NULL CHECK (attempt_number >= 1),
    status TEXT NOT NULL,
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    response_id TEXT,
    artifact_id TEXT,
    error_message TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (run_id) REFERENCES runs(run_id),
    FOREIGN KEY (project_id, artifact_id) REFERENCES artifacts(project_id, artifact_id)
);
"""


class Registry:
    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(path)
        self._connection.row_factory = sqlite3.Row
        self._connection.executescript(DDL)

    def close(self) -> None:
        self._connection.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        if self._connection.in_transaction:
            yield self._connection
            return
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            yield self._connection
        except BaseException:
            self._connection.rollback()
            raise
        else:
            self._connection.commit()

    def create_project(self, display_name: str, processing_profile: str) -> str:
        if processing_profile not in {"LOCAL_PROFILE", "CLOUD_PROFILE"}:
            raise ContractError("invalid processing profile")
        project_id = "prj_" + uuid.uuid4().hex
        self._connection.execute(
            "INSERT INTO projects VALUES (?, ?, ?, ?)",
            (project_id, display_name, utc_now(), processing_profile),
        )
        self._connection.commit()
        return project_id

    def project(self) -> sqlite3.Row:
        rows = self._connection.execute("SELECT * FROM projects ORDER BY created_at").fetchall()
        if len(rows) != 1:
            raise IntegrityError(f"project registry expected one project, found {len(rows)}")
        return cast(sqlite3.Row, rows[0])

    def register_source_asset(self, project_id: str, value: Mapping[str, Any]) -> None:
        required = {
            "source_asset_id",
            "asset_kind",
            "format",
            "content_hash",
            "byte_length",
            "storage_mode",
            "storage_locator",
            "registered_at",
        }
        missing = required.difference(value)
        if missing:
            raise ContractError(f"source asset missing fields: {sorted(missing)}")
        self._connection.execute(
            """
            INSERT OR IGNORE INTO source_assets
            (project_id, source_asset_id, asset_kind, media_kind, format, content_hash,
             byte_length, storage_mode, storage_locator, registered_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                project_id,
                value["source_asset_id"],
                value["asset_kind"],
                value.get("media_kind"),
                value["format"],
                value["content_hash"],
                value["byte_length"],
                value["storage_mode"],
                value["storage_locator"],
                value["registered_at"],
            ),
        )
        self._connection.commit()

    def source_asset(self, project_id: str, source_asset_id: str) -> sqlite3.Row:
        row = self._connection.execute(
            "SELECT * FROM source_assets WHERE project_id=? AND source_asset_id=?",
            (project_id, source_asset_id),
        ).fetchone()
        if row is None:
            raise IntegrityError(f"unknown source asset: {source_asset_id}")
        return cast(sqlite3.Row, row)

    def set_source_media_kind(
        self, project_id: str, source_asset_id: str, media_kind: str
    ) -> None:
        if media_kind not in {"audio", "video"}:
            raise ContractError("invalid source media kind")
        self._connection.execute(
            "UPDATE source_assets SET media_kind=? "
            "WHERE project_id=? AND source_asset_id=? AND media_kind IS NULL",
            (media_kind, project_id, source_asset_id),
        )
        self._connection.commit()

    def publish_artifact(
        self,
        *,
        project_id: str,
        envelope: ArtifactEnvelope,
        storage_locator: str,
        make_current: bool = True,
        stale_targets: Sequence[tuple[str, str | None]] = (),
    ) -> None:
        if not Path(storage_locator).is_file():
            raise IntegrityError("artifact file must exist before registry publication")
        with self.transaction() as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO artifacts
                (project_id, artifact_id, artifact_kind, scope_key, schema_version,
                 content_hash, storage_locator, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    project_id,
                    envelope.artifact_id,
                    envelope.artifact_kind,
                    envelope.scope_key,
                    envelope.schema_version,
                    envelope.content_hash,
                    storage_locator,
                    envelope.created_at,
                ),
            )
            for ordinal, item in enumerate(envelope.inputs):
                connection.execute(
                    """
                    INSERT OR IGNORE INTO artifact_dependencies
                    (project_id, artifact_id, ordinal, role, input_artifact_id,
                     input_source_asset_id, coordinate_range_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        project_id,
                        envelope.artifact_id,
                        ordinal,
                        item.role,
                        item.artifact_id,
                        item.source_asset_id,
                        json.dumps(item.coordinate_range, ensure_ascii=False, sort_keys=True)
                        if item.coordinate_range is not None
                        else None,
                    ),
                )
            for artifact_kind, scope_key in stale_targets:
                if scope_key is None:
                    connection.execute(
                        "UPDATE current_pointers SET is_stale=1 "
                        "WHERE project_id=? AND artifact_kind=?",
                        (project_id, artifact_kind),
                    )
                else:
                    connection.execute(
                        """
                        UPDATE current_pointers SET is_stale=1
                        WHERE project_id=? AND artifact_kind=? AND scope_key=?
                        """,
                        (project_id, artifact_kind, scope_key),
                    )
            if make_current:
                connection.execute(
                    """
                    INSERT INTO current_pointers
                    (project_id, artifact_kind, scope_key, artifact_id, is_stale, updated_at)
                    VALUES (?, ?, ?, ?, 0, ?)
                    ON CONFLICT(project_id, artifact_kind, scope_key) DO UPDATE SET
                      artifact_id=excluded.artifact_id,
                      is_stale=0,
                      updated_at=excluded.updated_at
                    """,
                    (
                        project_id,
                        envelope.artifact_kind,
                        envelope.scope_key,
                        envelope.artifact_id,
                        utc_now(),
                    ),
                )

    def current_pointer(
        self, project_id: str, artifact_kind: str, scope_key: str
    ) -> sqlite3.Row | None:
        return cast(
            sqlite3.Row | None,
            self._connection.execute(
            """
            SELECT p.*, a.storage_locator, a.content_hash, a.schema_version
            FROM current_pointers p
            JOIN artifacts a ON a.project_id=p.project_id AND a.artifact_id=p.artifact_id
            WHERE p.project_id=? AND p.artifact_kind=? AND p.scope_key=?
            """,
            (project_id, artifact_kind, scope_key),
            ).fetchone(),
        )

    def activate_artifacts(
        self,
        project_id: str,
        artifact_ids: Sequence[str],
        *,
        stale_targets: Sequence[tuple[str, str | None]] = (),
    ) -> None:
        """Atomically switch a complete stage result set to current."""
        with self.transaction() as connection:
            rows = connection.execute(
                f"SELECT artifact_id, artifact_kind, scope_key FROM artifacts "
                f"WHERE project_id=? AND artifact_id IN ({','.join('?' for _ in artifact_ids)})",
                (project_id, *artifact_ids),
            ).fetchall()
            if len(rows) != len(set(artifact_ids)):
                raise IntegrityError("cannot activate missing artifacts")
            for artifact_kind, scope_key in stale_targets:
                if scope_key is None:
                    connection.execute(
                        "UPDATE current_pointers SET is_stale=1 "
                        "WHERE project_id=? AND artifact_kind=?",
                        (project_id, artifact_kind),
                    )
                else:
                    connection.execute(
                        "UPDATE current_pointers SET is_stale=1 "
                        "WHERE project_id=? AND artifact_kind=? AND scope_key=?",
                        (project_id, artifact_kind, scope_key),
                    )
            for row in rows:
                connection.execute(
                    """
                    INSERT INTO current_pointers
                    (project_id, artifact_kind, scope_key, artifact_id, is_stale, updated_at)
                    VALUES (?, ?, ?, ?, 0, ?)
                    ON CONFLICT(project_id, artifact_kind, scope_key) DO UPDATE SET
                      artifact_id=excluded.artifact_id,
                      is_stale=0,
                      updated_at=excluded.updated_at
                    """,
                    (
                        project_id,
                        row["artifact_kind"],
                        row["scope_key"],
                        row["artifact_id"],
                        utc_now(),
                    ),
                )

    def current_pointers(
        self, project_id: str, artifact_kind: str | None = None
    ) -> list[sqlite3.Row]:
        if artifact_kind is None:
            return self._connection.execute(
                "SELECT * FROM current_pointers WHERE project_id=? "
                "ORDER BY artifact_kind, scope_key",
                (project_id,),
            ).fetchall()
        return self._connection.execute(
            """
            SELECT * FROM current_pointers
            WHERE project_id=? AND artifact_kind=? ORDER BY scope_key
            """,
            (project_id, artifact_kind),
        ).fetchall()

    def create_run(
        self, project_id: str, input_identity: Mapping[str, Any], config_hash: str
    ) -> str:
        run_id = "run_" + uuid.uuid4().hex
        now = utc_now()
        self._connection.execute(
            "INSERT INTO runs VALUES (?, ?, 'created', ?, ?, ?, ?, NULL)",
            (
                run_id,
                project_id,
                json.dumps(input_identity, ensure_ascii=False, sort_keys=True),
                config_hash,
                now,
                now,
            ),
        )
        self._connection.commit()
        return run_id

    def set_run_status(self, run_id: str, status: str, error_message: str | None = None) -> None:
        if status not in {"created", "running", "succeeded", "failed", "cancelled", "interrupted"}:
            raise ContractError("invalid run status")
        self._connection.execute(
            "UPDATE runs SET status=?, error_message=?, updated_at=? WHERE run_id=?",
            (status, error_message, utc_now(), run_id),
        )
        self._connection.commit()

    def run(self, run_id: str) -> sqlite3.Row:
        row = self._connection.execute(
            "SELECT * FROM runs WHERE run_id=?", (run_id,)
        ).fetchone()
        if row is None:
            raise IntegrityError(f"unknown run: {run_id}")
        return cast(sqlite3.Row, row)

    def latest_run(self, project_id: str) -> sqlite3.Row | None:
        return cast(
            sqlite3.Row | None,
            self._connection.execute(
                "SELECT * FROM runs WHERE project_id=? ORDER BY created_at DESC LIMIT 1",
                (project_id,),
            ).fetchone(),
        )

    def interrupt_running_runs(self) -> int:
        cursor = self._connection.execute(
            "UPDATE runs SET status='interrupted', updated_at=? WHERE status='running'",
            (utc_now(),),
        )
        self._connection.commit()
        return cursor.rowcount

    def create_invocation(
        self,
        *,
        run_id: str,
        project_id: str,
        operation: str,
        logical_operation_key: str,
        attempt_number: int,
        provider: str,
        model: str,
        chunk_id: str | None = None,
    ) -> str:
        invocation_id = "inv_" + uuid.uuid4().hex
        now = utc_now()
        self._connection.execute(
            """
            INSERT INTO invocations
            (invocation_id, run_id, project_id, chunk_id, operation, logical_operation_key,
             attempt_number, status, provider, model, response_id, artifact_id,
             error_message, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, 'created', ?, ?, NULL, NULL, NULL, ?, ?)
            """,
            (
                invocation_id,
                run_id,
                project_id,
                chunk_id,
                operation,
                logical_operation_key,
                attempt_number,
                provider,
                model,
                now,
                now,
            ),
        )
        self._connection.commit()
        return invocation_id

    def set_invocation_status(
        self,
        invocation_id: str,
        status: str,
        *,
        response_id: str | None = None,
        artifact_id: str | None = None,
        error_message: str | None = None,
    ) -> None:
        allowed = {
            "created",
            "sending",
            "succeeded",
            "definitely_not_sent",
            "delivery_ambiguous",
            "explicit_failure",
        }
        if status not in allowed:
            raise ContractError("invalid invocation status")
        self._connection.execute(
            """
            UPDATE invocations
            SET status=?, response_id=?, artifact_id=?, error_message=?, updated_at=?
            WHERE invocation_id=?
            """,
            (status, response_id, artifact_id, error_message, utc_now(), invocation_id),
        )
        self._connection.commit()

    def invocation(self, invocation_id: str) -> sqlite3.Row:
        row = self._connection.execute(
            "SELECT * FROM invocations WHERE invocation_id=?", (invocation_id,)
        ).fetchone()
        if row is None:
            raise IntegrityError(f"unknown invocation: {invocation_id}")
        return cast(sqlite3.Row, row)

    def invocations_for_run(self, run_id: str) -> list[sqlite3.Row]:
        return self._connection.execute(
            "SELECT * FROM invocations WHERE run_id=? ORDER BY created_at, invocation_id",
            (run_id,),
        ).fetchall()

    def dependencies(self, project_id: str, artifact_id: str) -> list[sqlite3.Row]:
        return self._connection.execute(
            """
            SELECT * FROM artifact_dependencies
            WHERE project_id=? AND artifact_id=? ORDER BY ordinal
            """,
            (project_id, artifact_id),
        ).fetchall()

    def orphan_candidates(self, artifacts_root: Path) -> list[Path]:
        registered = {
            Path(row[0]).resolve()
            for row in self._connection.execute("SELECT storage_locator FROM artifacts").fetchall()
        }
        return sorted(
            path
            for path in artifacts_root.rglob("*.json")
            if path.resolve() not in registered
        )
