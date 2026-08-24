from __future__ import annotations

import json
import sqlite3
import uuid
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any, cast

from cueflow.config import SEMANTIC_RETRY_RESET_LIMIT
from cueflow.errors import ContractError, IntegrityError
from cueflow.schema import ArtifactEnvelope, utc_now

REGISTRY_SCHEMA_VERSION = 1
REQUIRED_TABLES = frozenset(
    {
        "projects",
        "source_assets",
        "artifacts",
        "artifact_dependencies",
        "current_pointers",
        "runs",
        "invocations",
        "invocation_inputs",
        "semantic_budget_resets",
    }
)
SOURCE_ASSET_COLUMNS = (
    "project_id",
    "source_asset_id",
    "filename",
    "asset_kind",
    "media_kind",
    "format",
    "storage_mode",
    "storage_locator",
    "registered_at",
)

DDL = f"""
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
    filename TEXT NOT NULL,
    asset_kind TEXT NOT NULL CHECK (asset_kind IN ('media','auxiliary')),
    media_kind TEXT,
    format TEXT NOT NULL,
    storage_mode TEXT NOT NULL,
    storage_locator TEXT NOT NULL,
    registered_at TEXT NOT NULL,
    PRIMARY KEY (project_id, source_asset_id),
    UNIQUE (project_id, filename),
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
    semantic_budget_window INTEGER NOT NULL DEFAULT 0
        CHECK (semantic_budget_window BETWEEN 0 AND {SEMANTIC_RETRY_RESET_LIMIT}),
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

CREATE TABLE IF NOT EXISTS invocation_inputs (
    invocation_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    ordinal INTEGER NOT NULL,
    role TEXT NOT NULL,
    input_artifact_id TEXT NOT NULL,
    PRIMARY KEY (invocation_id, ordinal),
    FOREIGN KEY (invocation_id) REFERENCES invocations(invocation_id),
    FOREIGN KEY (project_id, input_artifact_id) REFERENCES artifacts(project_id, artifact_id)
);

CREATE TABLE IF NOT EXISTS semantic_budget_resets (
    run_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    chunk_id TEXT NOT NULL,
    window_index INTEGER NOT NULL
        CHECK (window_index BETWEEN 1 AND {SEMANTIC_RETRY_RESET_LIMIT}),
    trigger_invocation_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (run_id, chunk_id, window_index),
    UNIQUE (trigger_invocation_id),
    FOREIGN KEY (run_id) REFERENCES runs(run_id),
    FOREIGN KEY (trigger_invocation_id) REFERENCES invocations(invocation_id)
);

PRAGMA user_version = {REGISTRY_SCHEMA_VERSION};
"""


class Registry:
    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(path)
        self._connection.row_factory = sqlite3.Row
        try:
            self._initialize_or_validate_schema()
        except BaseException:
            self._connection.close()
            raise

    def _initialize_or_validate_schema(self) -> None:
        self._connection.execute("PRAGMA foreign_keys = ON")
        tables = {
            str(row[0])
            for row in self._connection.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            ).fetchall()
        }
        version_row = self._connection.execute("PRAGMA user_version").fetchone()
        assert version_row is not None
        version = int(version_row[0])
        if not tables and version == 0:
            self._connection.executescript(DDL)
            return
        if version != REGISTRY_SCHEMA_VERSION:
            raise IntegrityError(
                "unsupported CueFlow registry schema version: "
                f"expected {REGISTRY_SCHEMA_VERSION}, found {version}; migration is unavailable"
            )
        missing_tables = REQUIRED_TABLES.difference(tables)
        if missing_tables:
            raise IntegrityError(
                f"CueFlow registry schema is incomplete: missing tables {sorted(missing_tables)}"
            )
        source_columns = tuple(
            str(row[1])
            for row in self._connection.execute("PRAGMA table_info(source_assets)").fetchall()
        )
        if source_columns != SOURCE_ASSET_COLUMNS:
            raise IntegrityError("CueFlow registry SourceAsset schema does not match v0.1.1")

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

    def register_source_asset(
        self, project_id: str, value: Mapping[str, Any]
    ) -> sqlite3.Row:
        required = {
            "filename",
            "asset_kind",
            "format",
            "storage_mode",
            "storage_locator",
            "registered_at",
        }
        missing = required.difference(value)
        if missing:
            raise ContractError(f"source asset missing fields: {sorted(missing)}")
        existing = self.source_asset_by_filename(project_id, str(value["filename"]))
        if existing is not None:
            if existing["storage_locator"] != value["storage_locator"]:
                raise ContractError(
                    "source filename is already registered at a different locator; "
                    "Source relink is not available"
                )
            self._connection.execute(
                """
                UPDATE source_assets
                SET asset_kind=?, format=?
                WHERE project_id=? AND source_asset_id=?
                """,
                (
                    value["asset_kind"],
                    value["format"],
                    project_id,
                    existing["source_asset_id"],
                ),
            )
            self._connection.commit()
            return self.source_asset(project_id, str(existing["source_asset_id"]))
        source_asset_id = "src_" + uuid.uuid4().hex
        self._connection.execute(
            """
            INSERT INTO source_assets
            (project_id, source_asset_id, filename, asset_kind, media_kind, format,
             storage_mode, storage_locator, registered_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                project_id,
                source_asset_id,
                value["filename"],
                value["asset_kind"],
                value.get("media_kind"),
                value["format"],
                value["storage_mode"],
                value["storage_locator"],
                value["registered_at"],
            ),
        )
        self._connection.commit()
        return self.source_asset(project_id, source_asset_id)

    def source_asset_by_filename(
        self, project_id: str, filename: str
    ) -> sqlite3.Row | None:
        return cast(
            sqlite3.Row | None,
            self._connection.execute(
                "SELECT * FROM source_assets WHERE project_id=? AND filename=?",
                (project_id, filename),
            ).fetchone(),
        )

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
            "WHERE project_id=? AND source_asset_id=?",
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

    def artifact(self, project_id: str, artifact_id: str) -> sqlite3.Row:
        row = self._connection.execute(
            "SELECT * FROM artifacts WHERE project_id=? AND artifact_id=?",
            (project_id, artifact_id),
        ).fetchone()
        if row is None:
            raise IntegrityError(f"unknown artifact: {artifact_id}")
        return cast(sqlite3.Row, row)

    def dependent_artifacts(
        self, project_id: str, input_artifact_id: str, artifact_kind: str | None = None
    ) -> list[sqlite3.Row]:
        parameters: list[Any] = [project_id, input_artifact_id]
        kind_clause = ""
        if artifact_kind is not None:
            kind_clause = " AND a.artifact_kind=?"
            parameters.append(artifact_kind)
        return self._connection.execute(
            """
            SELECT a.* FROM artifact_dependencies d
            JOIN artifacts a ON a.project_id=d.project_id AND a.artifact_id=d.artifact_id
            WHERE d.project_id=? AND d.input_artifact_id=?
            """
            + kind_clause
            + " ORDER BY a.scope_key, a.created_at, a.artifact_id",
            parameters,
        ).fetchall()

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
        if status not in {"created", "running", "succeeded", "failed", "interrupted"}:
            raise ContractError("invalid run status")
        self._connection.execute(
            "UPDATE runs SET status=?, error_message=?, updated_at=? WHERE run_id=?",
            (status, error_message, utc_now(), run_id),
        )
        self._connection.commit()

    def reopen_run_for_retry(self, run_id: str) -> None:
        row = self.run(run_id)
        if row["status"] not in {"failed", "interrupted"}:
            raise ContractError("targeted retry requires a failed or interrupted Run")
        self.set_run_status(run_id, "running")

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

    def recover_running_runs(self) -> list[str]:
        run_ids = [
            str(row[0])
            for row in self._connection.execute(
                "SELECT run_id FROM runs WHERE status='running' ORDER BY created_at"
            ).fetchall()
        ]
        if not run_ids:
            return []
        with self.transaction() as connection:
            self._recover_inflight_invocations(
                connection,
                "run_id IN (SELECT run_id FROM runs WHERE status='running')",
                (),
                "previous Orchestrator stopped before Invocation completion",
            )
            connection.execute(
                """
                UPDATE runs
                SET status='interrupted',
                    error_message='previous Orchestrator execution was interrupted',
                    updated_at=?
                WHERE status='running'
                """,
                (utc_now(),),
            )
        return run_ids

    def finalize_interrupted_run(
        self, run_id: str, *, run_status: str, error_message: str
    ) -> None:
        if run_status not in {"failed", "interrupted"}:
            raise ContractError("interrupted execution must finish as failed or interrupted")
        with self.transaction() as connection:
            self._recover_inflight_invocations(
                connection,
                "run_id=?",
                (run_id,),
                error_message,
            )
            connection.execute(
                "UPDATE runs SET status=?, error_message=?, updated_at=? WHERE run_id=?",
                (run_status, error_message, utc_now(), run_id),
            )

    @staticmethod
    def _recover_inflight_invocations(
        connection: sqlite3.Connection,
        where_clause: str,
        parameters: Sequence[Any],
        error_message: str,
    ) -> None:
        connection.execute(
            f"""
            UPDATE invocations
            SET status=CASE status
                    WHEN 'created' THEN 'definitely_not_sent'
                    WHEN 'sending' THEN 'delivery_ambiguous'
                END,
                error_message=CASE status
                    WHEN 'created' THEN ?
                    WHEN 'sending' THEN ?
                END,
                updated_at=?
            WHERE {where_clause} AND status IN ('created', 'sending')
            """,
            (
                f"{error_message}; request was definitely not sent",
                f"{error_message}; delivery outcome is ambiguous",
                utc_now(),
                *parameters,
            ),
        )

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
        semantic_budget_window: int = 0,
        inputs: Sequence[tuple[str, str]] = (),
    ) -> str:
        if not 0 <= semantic_budget_window <= SEMANTIC_RETRY_RESET_LIMIT:
            raise ContractError("invalid semantic budget window")
        invocation_id = "inv_" + uuid.uuid4().hex
        now = utc_now()
        with self.transaction() as connection:
            connection.execute(
                """
                INSERT INTO invocations
                (invocation_id, run_id, project_id, chunk_id, operation, logical_operation_key,
                 attempt_number, semantic_budget_window, status, provider, model, response_id,
                 artifact_id, error_message, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'created', ?, ?, NULL, NULL, NULL, ?, ?)
                """,
                (
                    invocation_id,
                    run_id,
                    project_id,
                    chunk_id,
                    operation,
                    logical_operation_key,
                    attempt_number,
                    semantic_budget_window,
                    provider,
                    model,
                    now,
                    now,
                ),
            )
            for ordinal, (role, artifact_id) in enumerate(inputs):
                connection.execute(
                    """
                    INSERT INTO invocation_inputs
                    (invocation_id, project_id, ordinal, role, input_artifact_id)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (invocation_id, project_id, ordinal, role, artifact_id),
                )
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

    def invocation_inputs(self, invocation_id: str) -> list[sqlite3.Row]:
        return self._connection.execute(
            "SELECT * FROM invocation_inputs WHERE invocation_id=? ORDER BY ordinal",
            (invocation_id,),
        ).fetchall()

    def next_invocation_attempt_number(self, run_id: str, logical_operation_key: str) -> int:
        row = self._connection.execute(
            """
            SELECT COALESCE(MAX(attempt_number), 0) FROM invocations
            WHERE run_id=? AND logical_operation_key=?
            """,
            (run_id, logical_operation_key),
        ).fetchone()
        assert row is not None
        return int(row[0]) + 1

    def semantic_budget_window(self, run_id: str, chunk_id: str) -> int:
        row = self._connection.execute(
            """
            SELECT COALESCE(MAX(window_index), 0) FROM semantic_budget_resets
            WHERE run_id=? AND chunk_id=?
            """,
            (run_id, chunk_id),
        ).fetchone()
        assert row is not None
        return int(row[0])

    def sent_semantic_attempt_count(
        self, run_id: str, chunk_id: str, budget_window: int
    ) -> int:
        row = self._connection.execute(
            """
            SELECT COUNT(*) FROM invocations
            WHERE run_id=? AND chunk_id=? AND operation='semantic_transcription'
              AND semantic_budget_window=? AND status NOT IN ('created', 'definitely_not_sent')
            """,
            (run_id, chunk_id, budget_window),
        ).fetchone()
        assert row is not None
        return int(row[0])

    def record_semantic_budget_reset(
        self, run_id: str, project_id: str, chunk_id: str, trigger_invocation_id: str
    ) -> int:
        trigger = self.invocation(trigger_invocation_id)
        if (
            trigger["run_id"] != run_id
            or trigger["project_id"] != project_id
            or trigger["chunk_id"] != chunk_id
            or trigger["operation"] != "semantic_transcription"
            or trigger["status"]
            not in {"definitely_not_sent", "delivery_ambiguous", "explicit_failure"}
        ):
            raise ContractError("semantic budget reset trigger is invalid")
        current = self.semantic_budget_window(run_id, chunk_id)
        if current >= SEMANTIC_RETRY_RESET_LIMIT:
            raise ContractError("semantic retry reset limit exhausted")
        window = current + 1
        self._connection.execute(
            """
            INSERT INTO semantic_budget_resets
            (run_id, project_id, chunk_id, window_index, trigger_invocation_id, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (run_id, project_id, chunk_id, window, trigger_invocation_id, utc_now()),
        )
        self._connection.commit()
        return window

    def qa_repair_wave_count(self, run_id: str) -> int:
        row = self._connection.execute(
            "SELECT EXISTS(SELECT 1 FROM invocations "
            "WHERE run_id=? AND operation='qa_alignment_repair')",
            (run_id,),
        ).fetchone()
        assert row is not None
        return int(row[0])

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
