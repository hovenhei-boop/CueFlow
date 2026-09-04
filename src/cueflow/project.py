from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from functools import wraps
from pathlib import Path
from typing import Any, ParamSpec, TypeVar

from cueflow.artifact_store import ArtifactPublisher, ArtifactStore
from cueflow.errors import ContractError, IntegrityError, SourceMissingError
from cueflow.registry import Registry
from cueflow.schema import ArtifactEnvelope, utc_now

P = ParamSpec("P")
R = TypeVar("R")


def single_writer(function: Callable[P, R]) -> Callable[P, R]:
    """OS lock releases on process death; never recover a live writer's run."""

    @wraps(function)
    def guarded(*args: P.args, **kwargs: P.kwargs) -> R:
        context = args[0] if args else kwargs["context"]
        if not isinstance(context, ProjectContext):
            raise ContractError("writer requires ProjectContext")
        with (context.root / ".cueflow" / "writer.lock").open("a+b") as lock:
            import os

            if lock.tell() == 0:
                lock.write(b"0")
                lock.flush()
            lock.seek(0)
            try:
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    from importlib import import_module

                    fcntl = import_module("fcntl")

                    fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise ContractError("another CueFlow writer is active") from exc
            try:
                return function(*args, **kwargs)
            finally:
                lock.seek(0)
                if os.name == "nt":
                    msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    return guarded


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
    def create(cls, root: Path, display_name: str) -> ProjectContext:
        root.mkdir(parents=True, exist_ok=True)
        database = root / ".cueflow" / "registry.sqlite3"
        if database.exists():
            raise ContractError(f"project already exists: {root}")
        registry = Registry(database)
        project_id = registry.create_project(display_name)
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
        if asset_kind != "media":
            raise ContractError("v0.5.2 source assets must use asset_kind=media")
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

    def current_artifact(self, artifact_kind: str, scope_key: str = "global") -> ArtifactEnvelope:
        pointer = self.registry.current_pointer(self.project_id, artifact_kind, scope_key)
        if pointer is None or bool(pointer["is_stale"]):
            raise IntegrityError(f"missing or stale current Artifact: {artifact_kind}/{scope_key}")
        return self.artifact(str(pointer["artifact_id"]))

    def artifact(self, artifact_id: str) -> ArtifactEnvelope:
        row = self.registry.artifact(self.project_id, artifact_id)
        try:
            envelope = self.store.read_envelope(Path(str(row["storage_locator"])))
        except ContractError as exc:
            raise IntegrityError("stored artifact violates its schema/hash contract") from exc
        if envelope.artifact_id != artifact_id or envelope.content_hash != row["content_hash"]:
            raise IntegrityError("stored artifact identity does not match Registry")
        return envelope


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
