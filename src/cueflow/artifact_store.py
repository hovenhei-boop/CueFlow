from __future__ import annotations

import hashlib
import os
import tempfile
from collections.abc import Sequence
from pathlib import Path

from cueflow.canonical import canonical_bytes
from cueflow.errors import IntegrityError
from cueflow.registry import Registry
from cueflow.schema import ArtifactEnvelope


class ArtifactStore:
    def __init__(self, project_root: Path) -> None:
        self.project_root = project_root
        self.control_root = project_root / ".cueflow"
        self.artifacts_root = self.control_root / "artifacts" / "sha256"
        self.blobs_root = self.control_root / "blobs" / "sha256"
        self.temp_root = self.control_root / "tmp"
        for path in (self.artifacts_root, self.blobs_root, self.temp_root):
            path.mkdir(parents=True, exist_ok=True)

    def artifact_path(self, content_hash: str) -> Path:
        digest = _digest(content_hash)
        return self.artifacts_root / digest[:2] / f"{digest}.json"

    def blob_path(self, content_hash: str) -> Path:
        digest = _digest(content_hash)
        return self.blobs_root / digest[:2] / digest

    def write_envelope(self, envelope: ArtifactEnvelope) -> Path:
        destination = self.artifact_path(envelope.content_hash)
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            self.verify_envelope_file(destination, envelope)
            return destination
        payload = canonical_bytes(envelope.as_dict())
        temp_path = self._write_temp(payload, suffix=".json.tmp")
        os.replace(temp_path, destination)
        self.verify_envelope_file(destination, envelope)
        return destination

    def publish_blob(self, source: Path) -> tuple[str, int, Path]:
        digest = hashlib.sha256()
        byte_length = 0
        fd, raw_temp = tempfile.mkstemp(prefix="blob-", suffix=".tmp", dir=self.temp_root)
        temp_path = Path(raw_temp)
        try:
            with os.fdopen(fd, "wb") as output, source.open("rb") as input_file:
                while chunk := input_file.read(1024 * 1024):
                    digest.update(chunk)
                    byte_length += len(chunk)
                    output.write(chunk)
                output.flush()
                os.fsync(output.fileno())
            content_hash = "sha256:" + digest.hexdigest()
            destination = self.blob_path(content_hash)
            destination.parent.mkdir(parents=True, exist_ok=True)
            if destination.exists():
                temp_path.unlink(missing_ok=True)
            else:
                os.replace(temp_path, destination)
            self.verify_blob(destination, content_hash, byte_length)
            return content_hash, byte_length, destination
        except BaseException:
            temp_path.unlink(missing_ok=True)
            raise

    def read_envelope(self, path: Path) -> ArtifactEnvelope:
        import json

        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise IntegrityError(f"cannot read artifact envelope {path}: {exc}") from exc
        return ArtifactEnvelope.from_dict(value)

    def verify_envelope_file(self, path: Path, expected: ArtifactEnvelope) -> None:
        actual = self.read_envelope(path)
        if (
            actual.content_hash != expected.content_hash
            or actual.artifact_id != expected.artifact_id
        ):
            raise IntegrityError("artifact file content does not match expected envelope")

    def verify_blob(self, path: Path, content_hash: str, byte_length: int) -> None:
        digest = hashlib.sha256()
        actual_length = 0
        with path.open("rb") as input_file:
            while chunk := input_file.read(1024 * 1024):
                digest.update(chunk)
                actual_length += len(chunk)
        if actual_length != byte_length or "sha256:" + digest.hexdigest() != content_hash:
            raise IntegrityError("blob hash or byte length mismatch")

    def _write_temp(self, payload: bytes, suffix: str) -> Path:
        fd, raw_path = tempfile.mkstemp(prefix="artifact-", suffix=suffix, dir=self.temp_root)
        path = Path(raw_path)
        try:
            with os.fdopen(fd, "wb") as output:
                output.write(payload)
                output.flush()
                os.fsync(output.fileno())
            return path
        except BaseException:
            path.unlink(missing_ok=True)
            raise


class ArtifactPublisher:
    def __init__(self, registry: Registry, store: ArtifactStore, project_id: str) -> None:
        self.registry = registry
        self.store = store
        self.project_id = project_id

    def publish(
        self,
        envelope: ArtifactEnvelope,
        *,
        make_current: bool = True,
        stale_targets: Sequence[tuple[str, str | None]] = (),
    ) -> ArtifactEnvelope:
        path = self.store.write_envelope(envelope)
        self.registry.publish_artifact(
            project_id=self.project_id,
            envelope=envelope,
            storage_locator=str(path.resolve()),
            make_current=make_current,
            stale_targets=stale_targets,
        )
        return envelope


def _digest(content_hash: str) -> str:
    if not content_hash.startswith("sha256:") or len(content_hash) != 71:
        raise IntegrityError(f"invalid SHA-256 content hash: {content_hash}")
    return content_hash[7:]
