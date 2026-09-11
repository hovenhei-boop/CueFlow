from __future__ import annotations

import hashlib
import mimetypes
import os
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from importlib import import_module
from pathlib import Path
from typing import Any, Protocol, cast

from cueflow.config import TosConfig
from cueflow.errors import (
    ContractError,
    DeliveryAmbiguousError,
    ProviderError,
    ProviderUnavailableError,
)


@dataclass(frozen=True)
class MediaObjectRef:
    provider: str
    bucket: str
    object_key: str
    content_hash: str
    byte_length: int
    version_id: str | None = None
    mime_type: str = "application/octet-stream"

    def artifact_payload(self, timeline_audio_artifact_id: str) -> dict[str, object]:
        result: dict[str, object] = {
            "timeline_audio_artifact_id": timeline_audio_artifact_id,
            "provider": self.provider,
            "bucket": self.bucket,
            "object_key": self.object_key,
            "content_hash": self.content_hash,
            "byte_length": self.byte_length,
            "mime_type": self.mime_type,
        }
        if self.version_id is not None:
            result["version_id"] = self.version_id
        return result


class MediaObjectStore(Protocol):
    provider: str

    def upload(self, path: Path, *, object_name: str | None = None) -> MediaObjectRef: ...

    def plan_upload(self, path: Path, object_name: str) -> MediaObjectRef: ...

    def put(self, path: Path, ref: MediaObjectRef) -> MediaObjectRef: ...

    def head(self, ref: MediaObjectRef) -> MediaObjectRef | None: ...

    def materialize(self, ref: MediaObjectRef, destination: Path) -> None: ...

    def delete(self, ref: MediaObjectRef) -> None: ...

    def presign_get(self, ref: MediaObjectRef) -> str: ...

    def close(self) -> None: ...


class TosMediaObjectStore:
    provider = "volcengine-tos"

    def __init__(self, client: Any | None = None, config: TosConfig | None = None,
                 environment: Mapping[str, str] | None = None) -> None:
        self._client = client
        self._config = config or TosConfig()
        self._module: Any | None = None
        self._environment = dict(os.environ if environment is None else environment)

    def upload(self, path: Path, *, object_name: str | None = None) -> MediaObjectRef:
        return self.put(path, self.plan_upload(path, object_name or path.name))

    def plan_upload(self, path: Path, object_name: str) -> MediaObjectRef:
        endpoint = self._environment.get("TOS_ENDPOINT")
        region = self._environment.get("TOS_REGION")
        bucket = self._environment.get("TOS_BUCKET")
        if not endpoint or not region or not bucket:
            raise ProviderUnavailableError("TOS requires TOS_ENDPOINT, TOS_REGION, and TOS_BUCKET")
        digest, size = _hash_file(path, allow_empty=True)
        name = object_name
        if Path(name).name != name or not name:
            raise ContractError("TOS object_name must be one filename")
        object_key = f"{self._config.object_prefix}/{uuid.uuid4().hex}/{name}"
        return MediaObjectRef(self.provider, bucket, object_key, digest, size,
                              mime_type=mimetypes.guess_type(name)[0] or "application/octet-stream")

    def _ready_client(self) -> Any:
        endpoint = self._environment.get("TOS_ENDPOINT")
        region = self._environment.get("TOS_REGION")
        if not endpoint or not region:
            raise ProviderUnavailableError("TOS requires TOS_ENDPOINT and TOS_REGION")
        return self._client or self._make_client(endpoint, region)

    def put(self, path: Path, ref: MediaObjectRef) -> MediaObjectRef:
        if _hash_file(path, allow_empty=True) != (ref.content_hash, ref.byte_length):
            raise ContractError("upload source changed after intent creation")
        client = self._ready_client()
        try:
            result = client.put_object_from_file(
                ref.bucket, ref.object_key, str(path), forbid_overwrite=True,
                content_type=ref.mime_type,
                meta={"cueflow-sha256": ref.content_hash},
            )
        except Exception as exc:
            if _is_explicit_sdk_error(exc):
                raise ProviderError(f"TOS upload failed: {exc}") from exc
            raise DeliveryAmbiguousError(
                "TOS upload may have been delivered; automatic retry is forbidden"
            ) from exc
        version_id = getattr(result, "version_id", None)
        return MediaObjectRef(
            self.provider,
            ref.bucket,
            ref.object_key,
            ref.content_hash,
            ref.byte_length,
            str(version_id) if version_id else None,
            ref.mime_type,
        )

    def presign_get(self, ref: MediaObjectRef) -> str:
        endpoint = self._environment.get("TOS_ENDPOINT")
        region = self._environment.get("TOS_REGION")
        if not endpoint or not region:
            raise ProviderUnavailableError("TOS requires TOS_ENDPOINT and TOS_REGION")
        client = self._client or self._make_client(endpoint, region)
        module = self._tos_module()
        try:
            result = client.pre_signed_url(
                module.HttpMethodType.Http_Method_Get,
                ref.bucket,
                ref.object_key,
                expires=self._config.url_ttl_seconds,
                query={"versionId": ref.version_id} if ref.version_id else None,
            )
        except Exception as exc:
            raise ProviderError(f"TOS presign failed: {exc}") from exc
        signed_url = getattr(result, "signed_url", None)
        if not isinstance(signed_url, str) or not signed_url.startswith("https://"):
            raise ContractError("TOS returned an invalid presigned HTTPS URL")
        return signed_url

    def head(self, ref: MediaObjectRef) -> MediaObjectRef | None:
        try:
            result = self._ready_client().head_object(
                ref.bucket, ref.object_key, version_id=ref.version_id,
            )
        except Exception as exc:
            if getattr(exc, "status_code", None) == 404:
                return None
            raise ProviderError("TOS object verification failed") from exc
        if (int(result.content_length) != ref.byte_length
                or result.meta.get("cueflow-sha256") != ref.content_hash):
            raise ContractError("TOS object differs from persisted input identity")
        return MediaObjectRef(
            ref.provider, ref.bucket, ref.object_key, ref.content_hash, ref.byte_length,
            getattr(result, "version_id", None) or ref.version_id, ref.mime_type,
        )

    def materialize(self, ref: MediaObjectRef, destination: Path) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        try:
            self._ready_client().get_object_to_file(
                ref.bucket, ref.object_key, str(destination), version_id=ref.version_id,
            )
        except Exception as exc:
            raise ProviderError("TOS input materialization failed") from exc
        if _hash_file(destination, allow_empty=True) != (ref.content_hash, ref.byte_length):
            raise ContractError("downloaded object failed byte/hash verification")

    def close(self) -> None:
        close = getattr(self._client, "close", None)
        if callable(close):
            close()

    def delete(self, ref: MediaObjectRef) -> None:
        self._ready_client().delete_object(ref.bucket, ref.object_key, version_id=ref.version_id)

    def _make_client(self, endpoint: str, region: str) -> Any:
        access_key = self._environment.get("TOS_ACCESS_KEY")
        secret_key = self._environment.get("TOS_SECRET_KEY")
        if not access_key or not secret_key:
            raise ProviderUnavailableError("TOS requires TOS_ACCESS_KEY and TOS_SECRET_KEY")
        module = self._tos_module()
        self._client = module.TosClientV2(
            access_key, secret_key, endpoint, region, max_retry_count=0
        )
        return self._client

    def _tos_module(self) -> Any:
        if self._module is None:
            try:
                self._module = import_module("tos")
            except ImportError as exc:
                raise ProviderUnavailableError(
                    "TOS media upload requires the cueflow[cloud] dependencies"
                ) from exc
        return self._module


def media_ref_from_payload(value: dict[str, Any]) -> MediaObjectRef:
    return MediaObjectRef(
        provider=str(value["provider"]),
        bucket=str(value["bucket"]),
        object_key=str(value["object_key"]),
        content_hash=str(value["content_hash"]),
        byte_length=int(value["byte_length"]),
        version_id=str(value["version_id"]) if value.get("version_id") else None,
        mime_type=str(value.get("mime_type", "application/octet-stream")),
    )


def _hash_file(path: Path, *, allow_empty: bool = False) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    try:
        with path.open("rb") as stream:
            while block := stream.read(1024 * 1024):
                digest.update(block)
                size += len(block)
    except OSError as exc:
        raise ContractError(f"media source is unreadable: {path}") from exc
    if size == 0 and not allow_empty:
        raise ContractError("media source is empty")
    return "sha256:" + digest.hexdigest(), size


def _is_explicit_sdk_error(exc: Exception) -> bool:
    module = getattr(type(exc), "__module__", "")
    return cast(bool, module.startswith("tos.exceptions") and getattr(exc, "status_code", None))
