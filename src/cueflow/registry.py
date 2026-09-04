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

REGISTRY_SCHEMA_VERSION = 9
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
        "run_checkpoints",
    }
)
REQUIRED_TABLE_COLUMNS = {
    "run_checkpoints": ("run_id", "stage", "scope_key", "input_digest", "artifact_id", "revision"),
    "projects": ("project_id", "display_name", "created_at"),
    "source_assets": (
        "project_id",
        "source_asset_id",
        "filename",
        "asset_kind",
        "media_kind",
        "format",
        "storage_mode",
        "storage_locator",
        "registered_at",
    ),
    "artifacts": (
        "project_id",
        "artifact_id",
        "artifact_kind",
        "scope_key",
        "schema_version",
        "content_hash",
        "storage_locator",
        "created_at",
    ),
    "artifact_dependencies": (
        "project_id",
        "artifact_id",
        "ordinal",
        "role",
        "input_artifact_id",
        "input_source_asset_id",
        "coordinate_range_json",
    ),
    "current_pointers": (
        "project_id",
        "artifact_kind",
        "scope_key",
        "artifact_id",
        "storage_locator",
        "is_stale",
        "updated_at",
    ),
    "runs": (
        "run_id",
        "project_id",
        "operation_kind",
        "status",
        "source_asset_id",
        "job_input_artifact_id",
        "config_hash",
        "error_message",
        "created_at",
        "updated_at",
    ),
    "invocations": (
        "invocation_id",
        "run_id",
        "project_id",
        "operation",
        "logical_operation_key",
        "status",
        "provider",
        "requested_model",
        "resolved_model",
        "idempotency_key",
        "remote_job_id",
        "remote_status",
        "remote_artifact_refs_json",
        "response_id",
        "elapsed_ms",
        "reasoning_ms",
        "usage_json",
        "prompt_version",
        "prompt_sha256",
        "artifact_id",
        "error_message",
        "retry_of_invocation_id",
        "created_at",
        "updated_at",
    ),
    "invocation_inputs": (
        "invocation_id",
        "project_id",
        "ordinal",
        "role",
        "input_artifact_id",
    ),
}

_SCHEMA = f"""
CREATE TABLE projects (
    project_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE source_assets (
    project_id TEXT NOT NULL,
    source_asset_id TEXT NOT NULL,
    filename TEXT NOT NULL,
    asset_kind TEXT NOT NULL CHECK (asset_kind = 'media'),
    media_kind TEXT,
    format TEXT NOT NULL,
    storage_mode TEXT NOT NULL CHECK (storage_mode = 'external_reference'),
    storage_locator TEXT NOT NULL,
    registered_at TEXT NOT NULL,
    PRIMARY KEY (project_id, source_asset_id),
    UNIQUE (project_id, filename),
    FOREIGN KEY (project_id) REFERENCES projects(project_id)
);

CREATE TABLE artifacts (
    project_id TEXT NOT NULL,
    artifact_id TEXT NOT NULL,
    artifact_kind TEXT NOT NULL,
    scope_key TEXT NOT NULL,
    schema_version TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    storage_locator TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (project_id, artifact_id),
    UNIQUE (project_id, artifact_kind, scope_key, content_hash),
    FOREIGN KEY (project_id) REFERENCES projects(project_id)
);

CREATE TABLE artifact_dependencies (
    project_id TEXT NOT NULL,
    artifact_id TEXT NOT NULL,
    ordinal INTEGER NOT NULL,
    role TEXT NOT NULL,
    input_artifact_id TEXT,
    input_source_asset_id TEXT,
    coordinate_range_json TEXT,
    PRIMARY KEY (project_id, artifact_id, ordinal),
    CHECK ((input_artifact_id IS NOT NULL) != (input_source_asset_id IS NOT NULL)),
    FOREIGN KEY (project_id, artifact_id) REFERENCES artifacts(project_id, artifact_id),
    FOREIGN KEY (project_id, input_artifact_id) REFERENCES artifacts(project_id, artifact_id),
    FOREIGN KEY (project_id, input_source_asset_id)
        REFERENCES source_assets(project_id, source_asset_id)
);

CREATE TABLE current_pointers (
    project_id TEXT NOT NULL,
    artifact_kind TEXT NOT NULL,
    scope_key TEXT NOT NULL,
    artifact_id TEXT NOT NULL,
    storage_locator TEXT NOT NULL,
    is_stale INTEGER NOT NULL CHECK (is_stale IN (0, 1)),
    updated_at TEXT NOT NULL,
    PRIMARY KEY (project_id, artifact_kind, scope_key),
    FOREIGN KEY (project_id, artifact_id) REFERENCES artifacts(project_id, artifact_id)
);

CREATE TABLE runs (
    run_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    operation_kind TEXT NOT NULL CHECK (operation_kind IN ('run', 'correct')),
    status TEXT NOT NULL CHECK (
        status IN ('created', 'running', 'needs_review', 'succeeded', 'failed', 'interrupted')
    ),
    source_asset_id TEXT NOT NULL,
    job_input_artifact_id TEXT NOT NULL,
    config_hash TEXT NOT NULL,
    error_message TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (project_id) REFERENCES projects(project_id),
    FOREIGN KEY (project_id, source_asset_id)
        REFERENCES source_assets(project_id, source_asset_id),
    FOREIGN KEY (project_id, job_input_artifact_id)
        REFERENCES artifacts(project_id, artifact_id)
);

CREATE TABLE invocations (
    invocation_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    operation TEXT NOT NULL CHECK (
        operation IN (
            'media_upload', 'qwen_asr', 'doubao_asr', 'glm_asr',
            'qwen_correction', 'kimi_correction', 'ata'
        )
    ),
    logical_operation_key TEXT NOT NULL,
    status TEXT NOT NULL CHECK (
        status IN (
            'created', 'sending', 'succeeded', 'definitely_not_sent',
            'delivery_ambiguous', 'explicit_failure'
        )
    ),
    provider TEXT NOT NULL,
    requested_model TEXT,
    resolved_model TEXT,
    idempotency_key TEXT NOT NULL,
    remote_job_id TEXT,
    remote_status TEXT,
    remote_artifact_refs_json TEXT,
    response_id TEXT,
    elapsed_ms INTEGER,
    reasoning_ms INTEGER,
    usage_json TEXT,
    prompt_version TEXT,
    prompt_sha256 TEXT,
    artifact_id TEXT,
    error_message TEXT,
    retry_of_invocation_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (run_id) REFERENCES runs(run_id),
    FOREIGN KEY (project_id, artifact_id) REFERENCES artifacts(project_id, artifact_id),
    FOREIGN KEY (retry_of_invocation_id) REFERENCES invocations(invocation_id)
);

CREATE INDEX invocations_run_order ON invocations(run_id, created_at, invocation_id);

CREATE TABLE invocation_inputs (
    invocation_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    ordinal INTEGER NOT NULL,
    role TEXT NOT NULL,
    input_artifact_id TEXT NOT NULL,
    PRIMARY KEY (invocation_id, ordinal),
    FOREIGN KEY (invocation_id) REFERENCES invocations(invocation_id),
    FOREIGN KEY (project_id, input_artifact_id)
        REFERENCES artifacts(project_id, artifact_id)
);

CREATE TABLE run_checkpoints (
    run_id TEXT NOT NULL REFERENCES runs(run_id),
    stage TEXT NOT NULL,
    scope_key TEXT NOT NULL,
    input_digest TEXT NOT NULL,
    artifact_id TEXT NOT NULL,
    revision INTEGER NOT NULL CHECK(revision > 0),
    PRIMARY KEY(run_id, stage, scope_key)
);
PRAGMA user_version = {REGISTRY_SCHEMA_VERSION};
"""


class Registry:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.connection = sqlite3.connect(path)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        version = int(self.connection.execute("PRAGMA user_version").fetchone()[0])
        tables = self._table_names()
        if version == 0 and not tables:
            self.connection.executescript(_SCHEMA)
            self.connection.commit()
        elif version != REGISTRY_SCHEMA_VERSION:
            self.connection.close()
            raise IntegrityError(
                "incompatible Registry schema: "
                f"expected {REGISTRY_SCHEMA_VERSION}, found {version}; "
                "CueFlow v0.5.2 does not migrate older projects"
            )
        actual_tables = self._table_names()
        if actual_tables != REQUIRED_TABLES:
            self.connection.close()
            raise IntegrityError(
                "Registry tables do not match the current contract: "
                f"expected {sorted(REQUIRED_TABLES)}, found {sorted(actual_tables)}"
            )
        for table, expected in REQUIRED_TABLE_COLUMNS.items():
            actual = self._table_columns(table)
            if actual != expected:
                self.connection.close()
                raise IntegrityError(
                    f"Registry columns do not match for {table}: "
                    f"expected {list(expected)}, found {list(actual)}"
                )

    def close(self) -> None:
        self.connection.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        nested = self.connection.in_transaction
        savepoint = "nested_" + uuid.uuid4().hex
        try:
            self.connection.execute(f"SAVEPOINT {savepoint}" if nested else "BEGIN IMMEDIATE")
            yield self.connection
        except BaseException:
            if nested:
                self.connection.execute(f"ROLLBACK TO {savepoint}")
                self.connection.execute(f"RELEASE {savepoint}")
            else:
                self.connection.rollback()
            raise
        else:
            if nested:
                self.connection.execute(f"RELEASE {savepoint}")
            else:
                self.connection.commit()

    def create_project(self, display_name: str) -> str:
        if not display_name.strip():
            raise ContractError("project display name must not be empty")
        project_id = "prj_" + uuid.uuid4().hex
        self.connection.execute(
            "INSERT INTO projects VALUES (?, ?, ?)",
            (project_id, display_name, utc_now()),
        )
        self.connection.commit()
        return project_id

    def project(self) -> sqlite3.Row:
        rows = self.connection.execute("SELECT * FROM projects").fetchall()
        if len(rows) != 1:
            raise IntegrityError("Registry must contain exactly one project")
        return cast(sqlite3.Row, rows[0])

    def register_source_asset(self, project_id: str, value: Mapping[str, Any]) -> sqlite3.Row:
        existing = self.connection.execute(
            "SELECT * FROM source_assets WHERE project_id=? AND filename=? COLLATE BINARY",
            (project_id, str(value["filename"])),
        ).fetchone()
        if existing is not None:
            return cast(sqlite3.Row, existing)
        source_asset_id = "src_" + uuid.uuid4().hex
        self.connection.execute(
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
        self.connection.commit()
        return self.source_asset(project_id, source_asset_id)

    def source_asset(self, project_id: str, source_asset_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM source_assets WHERE project_id=? AND source_asset_id=?",
            (project_id, source_asset_id),
        ).fetchone()
        if row is None:
            raise IntegrityError(f"unknown SourceAsset: {source_asset_id}")
        return cast(sqlite3.Row, row)

    def set_source_media_kind(self, project_id: str, source_asset_id: str, media_kind: str) -> None:
        if media_kind not in {"audio", "video"}:
            raise ContractError("invalid source media kind")
        cursor = self.connection.execute(
            "UPDATE source_assets SET media_kind=? WHERE project_id=? AND source_asset_id=?",
            (media_kind, project_id, source_asset_id),
        )
        if cursor.rowcount != 1:
            raise IntegrityError(f"unknown SourceAsset: {source_asset_id}")
        self.connection.commit()

    def publish_artifact(
        self,
        *,
        project_id: str,
        envelope: ArtifactEnvelope,
        storage_locator: str,
        make_current: bool,
        stale_targets: Sequence[tuple[str, str | None]],
        checkpoint: tuple[str, str, str, str] | None = None,
        invocation_id: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> None:
        with self.transaction() as tx:
            existing = tx.execute(
                "SELECT * FROM artifacts WHERE project_id=? AND artifact_id=?",
                (project_id, envelope.artifact_id),
            ).fetchone()
            if existing is None:
                tx.execute(
                    "INSERT INTO artifacts VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
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
                self._insert_dependencies(tx, project_id, envelope)
            elif (
                existing["content_hash"] != envelope.content_hash
                or existing["storage_locator"] != storage_locator
            ):
                raise IntegrityError("Artifact identity collides with different stored content")
            self._stale(tx, project_id, stale_targets)
            if make_current:
                self._activate(tx, project_id, envelope.artifact_id)
            if checkpoint is not None:
                self._checkpoint_tx(tx, *checkpoint, envelope.artifact_id)
            if invocation_id is not None:
                row = self.invocation(invocation_id)
                if row["project_id"] != project_id or checkpoint is None:
                    raise IntegrityError("completion must bind a project and run checkpoint")
                if row["run_id"] != checkpoint[0] or row["status"] != "sending":
                    raise IntegrityError("invalid invocation completion")
                details = metadata or {}
                tx.execute(
                    """UPDATE invocations SET status='succeeded', artifact_id=?,
                    resolved_model=?, response_id=?, elapsed_ms=?, reasoning_ms=?,
                    usage_json=?, error_message=NULL, updated_at=? WHERE invocation_id=?""",
                    (
                        envelope.artifact_id,
                        details.get("resolved_model"),
                        details.get("response_id"),
                        details.get("elapsed_ms"),
                        details.get("reasoning_ms"),
                        json.dumps(details.get("usage"), ensure_ascii=False),
                        utc_now(),
                        invocation_id,
                    ),
                )

    def _checkpoint_tx(
        self,
        tx: sqlite3.Connection,
        run_id: str,
        stage: str,
        scope: str,
        digest: str,
        artifact_id: str,
    ) -> None:
        run = self.run(run_id)
        artifact = self.artifact(str(run["project_id"]), artifact_id)
        if artifact["artifact_kind"] != stage or artifact["scope_key"] != scope:
            raise IntegrityError("checkpoint kind/scope must match its artifact")
        prior = self.checkpoint(run_id, stage, scope)
        if prior is not None and prior["input_digest"] != digest:
            raise IntegrityError("checkpoint input identity changed inside a run")
        if prior is not None and prior["artifact_id"] == artifact_id:
            return
        tx.execute(
            """INSERT INTO run_checkpoints VALUES (?, ?, ?, ?, ?, 1)
            ON CONFLICT(run_id, stage, scope_key) DO UPDATE SET
                artifact_id=excluded.artifact_id, revision=run_checkpoints.revision+1""",
            (run_id, stage, scope, digest, artifact_id),
        )

    def checkpoint(self, run_id: str, stage: str, scope: str = "global") -> sqlite3.Row | None:
        return cast(
            sqlite3.Row | None,
            self.connection.execute(
                "SELECT * FROM run_checkpoints WHERE run_id=? AND stage=? AND scope_key=?",
                (run_id, stage, scope),
            ).fetchone(),
        )

    def bind_checkpoint(
        self,
        run_id: str,
        stage: str,
        artifact_id: str,
        digest: str,
        scope: str = "global",
    ) -> None:
        with self.transaction() as tx:
            self._checkpoint_tx(tx, run_id, stage, scope, digest, artifact_id)

    def activate_artifacts(
        self,
        project_id: str,
        artifact_ids: Sequence[str],
        stale_targets: Sequence[tuple[str, str | None]] = (),
    ) -> None:
        with self.transaction() as tx:
            self._stale(tx, project_id, stale_targets)
            for artifact_id in artifact_ids:
                self._activate(tx, project_id, artifact_id)

    def artifact(self, project_id: str, artifact_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM artifacts WHERE project_id=? AND artifact_id=?",
            (project_id, artifact_id),
        ).fetchone()
        if row is None:
            raise IntegrityError(f"unknown Artifact: {artifact_id}")
        return cast(sqlite3.Row, row)

    def current_pointer(
        self, project_id: str, artifact_kind: str, scope_key: str
    ) -> sqlite3.Row | None:
        return cast(
            sqlite3.Row | None,
            self.connection.execute(
                """
            SELECT * FROM current_pointers
            WHERE project_id=? AND artifact_kind=? AND scope_key=?
            """,
                (project_id, artifact_kind, scope_key),
            ).fetchone(),
        )

    def current_artifacts(self, project_id: str) -> list[sqlite3.Row]:
        return self.connection.execute(
            """
            SELECT * FROM current_pointers WHERE project_id=?
            ORDER BY artifact_kind, scope_key
            """,
            (project_id,),
        ).fetchall()

    def dependencies(self, project_id: str, artifact_id: str) -> list[sqlite3.Row]:
        return self.connection.execute(
            """
            SELECT * FROM artifact_dependencies
            WHERE project_id=? AND artifact_id=? ORDER BY ordinal
            """,
            (project_id, artifact_id),
        ).fetchall()

    def dependent_artifacts(
        self, project_id: str, input_artifact_id: str, artifact_kind: str
    ) -> list[sqlite3.Row]:
        return self.connection.execute(
            """
            SELECT a.* FROM artifacts a
            JOIN artifact_dependencies d
              ON d.project_id=a.project_id AND d.artifact_id=a.artifact_id
            WHERE a.project_id=? AND d.input_artifact_id=? AND a.artifact_kind=?
            ORDER BY a.created_at, a.artifact_id
            """,
            (project_id, input_artifact_id, artifact_kind),
        ).fetchall()

    def create_source_run(
        self,
        project_id: str,
        *,
        operation_kind: str,
        source_asset_id: str,
        job_input_artifact_id: str,
        config_hash: str,
    ) -> str:
        if operation_kind not in {"run", "correct"}:
            raise ContractError("invalid source Run operation kind")
        self.source_asset(project_id, source_asset_id)
        self.artifact(project_id, job_input_artifact_id)
        run_id = "run_" + uuid.uuid4().hex
        now = utc_now()
        self.connection.execute(
            "INSERT INTO runs VALUES (?, ?, ?, 'created', ?, ?, ?, NULL, ?, ?)",
            (
                run_id,
                project_id,
                operation_kind,
                source_asset_id,
                job_input_artifact_id,
                config_hash,
                now,
                now,
            ),
        )
        self.connection.commit()
        return run_id

    def run(self, run_id: str) -> sqlite3.Row:
        row = self.connection.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
        if row is None:
            raise IntegrityError(f"unknown Run: {run_id}")
        return cast(sqlite3.Row, row)

    def runs(self, project_id: str) -> list[sqlite3.Row]:
        return self.connection.execute(
            "SELECT * FROM runs WHERE project_id=? ORDER BY created_at, run_id",
            (project_id,),
        ).fetchall()

    def set_run_status(self, run_id: str, status: str, *, error_message: str | None = None) -> None:
        if status not in {"running", "needs_review", "succeeded", "failed", "interrupted"}:
            raise ContractError("invalid Run status transition target")
        cursor = self.connection.execute(
            "UPDATE runs SET status=?, error_message=?, updated_at=? WHERE run_id=?",
            (status, error_message, utc_now(), run_id),
        )
        if cursor.rowcount != 1:
            raise IntegrityError(f"unknown Run: {run_id}")
        self.connection.commit()

    def reopen_run_for_retry(self, run_id: str) -> None:
        row = self.run(run_id)
        if row["status"] not in {"failed", "interrupted", "needs_review"}:
            raise ContractError("only a failed, interrupted, or review-pending Run can resume")
        self.set_run_status(run_id, "running")

    def finalize_interrupted_run(self, run_id: str, *, run_status: str, error_message: str) -> None:
        if run_status not in {"failed", "interrupted"}:
            raise ContractError("invalid interrupted Run terminal status")
        self.set_run_status(run_id, run_status, error_message=error_message)

    def recover_running_source_runs(self) -> list[str]:
        rows = self.connection.execute(
            "SELECT run_id FROM runs WHERE status='running' ORDER BY created_at"
        ).fetchall()
        recovered = [str(row["run_id"]) for row in rows]
        if not recovered:
            return []
        now = utc_now()
        with self.transaction() as tx:
            for run_id in recovered:
                tx.execute(
                    """
                    UPDATE invocations
                    SET status='definitely_not_sent',
                        error_message='recovered before provider delivery', updated_at=?
                    WHERE run_id=? AND status='created'
                    """,
                    (now, run_id),
                )
                tx.execute(
                    """
                    UPDATE invocations
                    SET status='delivery_ambiguous',
                        error_message='recovered after provider delivery began', updated_at=?
                    WHERE run_id=? AND status='sending'
                    """,
                    (now, run_id),
                )
                tx.execute(
                    """
                    UPDATE runs SET status='interrupted',
                        error_message='recovered interrupted Run', updated_at=?
                    WHERE run_id=?
                    """,
                    (now, run_id),
                )
        return recovered

    def create_invocation(
        self,
        *,
        run_id: str,
        project_id: str,
        operation: str,
        logical_operation_key: str,
        provider: str,
        requested_model: str | None,
        idempotency_key: str,
        inputs: Sequence[tuple[str, str]],
        prompt_version: str | None = None,
        prompt_sha256: str | None = None,
        retry_of_invocation_id: str | None = None,
    ) -> str:
        self.run(run_id)
        invocation_id = "inv_" + uuid.uuid4().hex
        now = utc_now()
        with self.transaction() as tx:
            tx.execute(
                """
                INSERT INTO invocations
                (invocation_id, run_id, project_id, operation, logical_operation_key,
                 status, provider, requested_model, resolved_model, idempotency_key,
                 remote_job_id, remote_status, remote_artifact_refs_json, response_id,
                 elapsed_ms, reasoning_ms, usage_json, prompt_version, prompt_sha256,
                 artifact_id, error_message, retry_of_invocation_id, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, 'created', ?, ?, NULL, ?, NULL, NULL, NULL, NULL,
                        NULL, NULL, NULL, ?, ?, NULL, NULL, ?, ?, ?)
                """,
                (
                    invocation_id,
                    run_id,
                    project_id,
                    operation,
                    logical_operation_key,
                    provider,
                    requested_model,
                    idempotency_key,
                    prompt_version,
                    prompt_sha256,
                    retry_of_invocation_id,
                    now,
                    now,
                ),
            )
            for ordinal, (role, artifact_id) in enumerate(inputs):
                self.artifact(project_id, artifact_id)
                tx.execute(
                    "INSERT INTO invocation_inputs VALUES (?, ?, ?, ?, ?)",
                    (invocation_id, project_id, ordinal, role, artifact_id),
                )
        return invocation_id

    def invocation(self, invocation_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM invocations WHERE invocation_id=?", (invocation_id,)
        ).fetchone()
        if row is None:
            raise IntegrityError(f"unknown Invocation: {invocation_id}")
        return cast(sqlite3.Row, row)

    def invocations_for_run(self, run_id: str) -> list[sqlite3.Row]:
        return self.connection.execute(
            "SELECT * FROM invocations WHERE run_id=? ORDER BY created_at, invocation_id",
            (run_id,),
        ).fetchall()

    def invocation_inputs(self, invocation_id: str) -> list[sqlite3.Row]:
        return self.connection.execute(
            "SELECT * FROM invocation_inputs WHERE invocation_id=? ORDER BY ordinal",
            (invocation_id,),
        ).fetchall()

    def set_invocation_status(
        self,
        invocation_id: str,
        status: str,
        *,
        artifact_id: str | None = None,
        response_id: str | None = None,
        resolved_model: str | None = None,
        elapsed_ms: int | None = None,
        reasoning_ms: int | None = None,
        usage: Mapping[str, Any] | None = None,
        error_message: str | None = None,
    ) -> None:
        if status not in {
            "sending",
            "succeeded",
            "definitely_not_sent",
            "delivery_ambiguous",
            "explicit_failure",
        }:
            raise ContractError("invalid Invocation status")
        cursor = self.connection.execute(
            """
            UPDATE invocations SET status=?, artifact_id=COALESCE(?, artifact_id),
                response_id=COALESCE(?, response_id),
                resolved_model=COALESCE(?, resolved_model),
                elapsed_ms=COALESCE(?, elapsed_ms),
                reasoning_ms=COALESCE(?, reasoning_ms),
                usage_json=COALESCE(?, usage_json), error_message=?, updated_at=?
            WHERE invocation_id=?
            """,
            (
                status,
                artifact_id,
                response_id,
                resolved_model,
                elapsed_ms,
                reasoning_ms,
                json.dumps(dict(usage), ensure_ascii=False, separators=(",", ":"))
                if usage is not None
                else None,
                error_message,
                utc_now(),
                invocation_id,
            ),
        )
        if cursor.rowcount != 1:
            raise IntegrityError(f"unknown Invocation: {invocation_id}")
        self.connection.commit()

    def update_remote_job(
        self,
        invocation_id: str,
        *,
        remote_job_id: str | None = None,
        remote_status: str | None = None,
        remote_artifact_refs: Sequence[Mapping[str, Any]] | None = None,
    ) -> None:
        row = self.invocation(invocation_id)
        refs = (
            json.dumps(list(remote_artifact_refs), ensure_ascii=False, separators=(",", ":"))
            if remote_artifact_refs is not None
            else row["remote_artifact_refs_json"]
        )
        self.connection.execute(
            """
            UPDATE invocations
            SET remote_job_id=?, remote_status=?, remote_artifact_refs_json=?, updated_at=?
            WHERE invocation_id=?
            """,
            (
                remote_job_id or row["remote_job_id"],
                remote_status or row["remote_status"],
                refs,
                utc_now(),
                invocation_id,
            ),
        )
        self.connection.commit()

    def _insert_dependencies(
        self, tx: sqlite3.Connection, project_id: str, envelope: ArtifactEnvelope
    ) -> None:
        for ordinal, item in enumerate(envelope.inputs):
            tx.execute(
                """
                INSERT INTO artifact_dependencies
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
                    (
                        json.dumps(dict(item.coordinate_range), separators=(",", ":"))
                        if item.coordinate_range is not None
                        else None
                    ),
                ),
            )

    def _activate(self, tx: sqlite3.Connection, project_id: str, artifact_id: str) -> None:
        row = tx.execute(
            "SELECT * FROM artifacts WHERE project_id=? AND artifact_id=?",
            (project_id, artifact_id),
        ).fetchone()
        if row is None:
            raise IntegrityError(f"unknown Artifact: {artifact_id}")
        tx.execute(
            """
            INSERT INTO current_pointers
            (project_id, artifact_kind, scope_key, artifact_id, storage_locator,
             is_stale, updated_at)
            VALUES (?, ?, ?, ?, ?, 0, ?)
            ON CONFLICT(project_id, artifact_kind, scope_key) DO UPDATE SET
                artifact_id=excluded.artifact_id,
                storage_locator=excluded.storage_locator,
                is_stale=0,
                updated_at=excluded.updated_at
            """,
            (
                project_id,
                row["artifact_kind"],
                row["scope_key"],
                artifact_id,
                row["storage_locator"],
                utc_now(),
            ),
        )

    def _stale(
        self,
        tx: sqlite3.Connection,
        project_id: str,
        targets: Sequence[tuple[str, str | None]],
    ) -> None:
        for kind, scope in targets:
            if scope is None:
                tx.execute(
                    "UPDATE current_pointers SET is_stale=1, updated_at=? "
                    "WHERE project_id=? AND artifact_kind=?",
                    (utc_now(), project_id, kind),
                )
            else:
                tx.execute(
                    "UPDATE current_pointers SET is_stale=1, updated_at=? "
                    "WHERE project_id=? AND artifact_kind=? AND scope_key=?",
                    (utc_now(), project_id, kind, scope),
                )

    def _table_names(self) -> frozenset[str]:
        rows = self.connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
        return frozenset(str(row[0]) for row in rows)

    def _table_columns(self, table: str) -> tuple[str, ...]:
        if table not in REQUIRED_TABLES:
            raise IntegrityError(f"unknown Registry table: {table}")
        rows = self.connection.execute(f"PRAGMA table_info({table})").fetchall()
        return tuple(str(row[1]) for row in rows)
