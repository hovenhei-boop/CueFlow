from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cueflow.artifact_store import ArtifactPublisher, ArtifactStore
from cueflow.config import PROFILES
from cueflow.errors import ContractError, IntegrityError, SourceMissingError
from cueflow.registry import Registry
from cueflow.schema import ArtifactEnvelope, utc_now


@dataclass
class ProjectContext:
    root: Path
    registry: Registry
    store: ArtifactStore
    project_id: str

    @property
    def publisher(self) -> ArtifactPublisher:
        return ArtifactPublisher(self.registry, self.store, self.project_id)

    @classmethod
    def create(cls, root: Path, display_name: str, profile: str) -> ProjectContext:
        if profile not in PROFILES:
            raise ContractError("profile must be LOCAL_PROFILE or CLOUD_PROFILE")
        root.mkdir(parents=True, exist_ok=True)
        database = root / ".cueflow" / "registry.sqlite3"
        if database.exists():
            raise ContractError(f"project already exists: {root}")
        registry = Registry(database)
        project_id = registry.create_project(display_name, profile)
        store = ArtifactStore(root)
        (root / "output").mkdir(parents=True, exist_ok=True)
        return cls(root=root, registry=registry, store=store, project_id=project_id)

    @classmethod
    def open(cls, root: Path) -> ProjectContext:
        database = root / ".cueflow" / "registry.sqlite3"
        if not database.is_file():
            raise IntegrityError(f"not a CueFlow project: {root}")
        registry = Registry(database)
        registry.interrupt_running_runs()
        project_id = str(registry.project()["project_id"])
        return cls(root=root, registry=registry, store=ArtifactStore(root), project_id=project_id)

    def close(self) -> None:
        self.registry.close()

    def register_external_asset(
        self, path: Path, *, asset_kind: str, media_kind: str | None = None
    ) -> dict[str, Any]:
        if not path.is_file():
            if asset_kind == "media":
                raise SourceMissingError(f"source_missing: {path}")
            raise ContractError(f"source file does not exist: {path}")
        if asset_kind not in {"media", "auxiliary"}:
            raise ContractError("asset_kind must be media or auxiliary")
        digest = hashlib.sha256()
        byte_length = 0
        with path.open("rb") as input_file:
            while chunk := input_file.read(1024 * 1024):
                digest.update(chunk)
                byte_length += len(chunk)
        content_hash = "sha256:" + digest.hexdigest()
        value: dict[str, Any] = {
            "source_asset_id": "src_" + digest.hexdigest(),
            "asset_kind": asset_kind,
            "media_kind": media_kind,
            "format": path.suffix.lower().lstrip(".") or "unknown",
            "content_hash": content_hash,
            "byte_length": byte_length,
            "storage_mode": "external_reference",
            "storage_locator": str(path.resolve()),
            "registered_at": utc_now(),
        }
        self.registry.register_source_asset(self.project_id, value)
        return value

    def verify_external_asset(self, source_asset_id: str) -> Path:
        row = self.registry.source_asset(self.project_id, source_asset_id)
        path = Path(str(row["storage_locator"]))
        if not path.is_file():
            raise SourceMissingError(f"source_missing: {path}")
        digest = hashlib.sha256()
        byte_length = 0
        with path.open("rb") as input_file:
            while chunk := input_file.read(1024 * 1024):
                digest.update(chunk)
                byte_length += len(chunk)
        content_changed = "sha256:" + digest.hexdigest() != row["content_hash"]
        if byte_length != row["byte_length"] or content_changed:
            raise IntegrityError("external source content no longer matches SourceAsset identity")
        return path

    def current_artifact(
        self, artifact_kind: str, scope_key: str = "global"
    ) -> ArtifactEnvelope:
        pointer = self.registry.current_pointer(
            self.project_id, artifact_kind, scope_key
        )
        if pointer is None or bool(pointer["is_stale"]):
            raise IntegrityError(f"missing or stale current Artifact: {artifact_kind}/{scope_key}")
        return self.store.read_envelope(Path(str(pointer["storage_locator"])))

    def artifact(self, artifact_id: str) -> ArtifactEnvelope:
        row = self.registry.artifact(self.project_id, artifact_id)
        return self.store.read_envelope(Path(str(row["storage_locator"])))
