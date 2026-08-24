from __future__ import annotations

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
        project_id = str(registry.project()["project_id"])
        return cls(root=root, registry=registry, store=ArtifactStore(root), project_id=project_id)

    def close(self) -> None:
        self.registry.close()

    def register_external_asset(
        self, path: Path, *, asset_kind: str, media_kind: str | None = None
    ) -> dict[str, Any]:
        if asset_kind not in {"media", "auxiliary"}:
            raise ContractError("asset_kind must be media or auxiliary")
        resolved = _readable_source_path(path)
        value: dict[str, Any] = {
            "filename": path.name,
            "asset_kind": asset_kind,
            "media_kind": media_kind,
            "format": path.suffix.lower().lstrip(".") or "unknown",
            "storage_mode": "external_reference",
            "storage_locator": str(resolved),
            "registered_at": utc_now(),
        }
        return dict(self.registry.register_source_asset(self.project_id, value))

    def verify_external_asset(self, source_asset_id: str) -> Path:
        row = self.registry.source_asset(self.project_id, source_asset_id)
        path = Path(str(row["storage_locator"]))
        return _readable_source_path(path)

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


def _readable_source_path(path: Path) -> Path:
    try:
        if not path.is_file():
            raise SourceMissingError(f"source_missing: {path}")
        with path.open("rb"):
            pass
        return path.resolve()
    except SourceMissingError:
        raise
    except OSError as exc:
        raise SourceMissingError(f"source_missing: {path}") from exc
