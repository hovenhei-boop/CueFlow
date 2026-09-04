from __future__ import annotations

import hashlib
import os
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

    def artifact_payload(self, source_asset_id: str) -> dict[str, object]:
        result: dict[str, object] = {
            "source_asset_id": source_asset_id,
            "provider": self.provider,
            "bucket": self.bucket,
            "object_key": self.object_key,
            "content_hash": self.content_hash,
            "byte_length": self.byte_length,
        }
        if self.version_id is not None:
            result["version_id"] = self.version_id
        return result


class MediaObjectStore(Protocol):
    provider: str

    def upload(self, path: Path) -> MediaObjectRef: ...

    def presign_get(self, ref: MediaObjectRef) -> str: ...

    def close(self) -> None: ...


class TosMediaObjectStore:
    provider = "volcengine-tos"

    def __init__(self, client: Any | None = None, config: TosConfig | None = None) -> None:
        self._client = client
        self._config = config or TosConfig()
        self._module: Any | None = None

    def upload(self, path: Path) -> MediaObjectRef:
        endpoint = os.getenv("TOS_ENDPOINT")
        region = os.getenv("TOS_REGION")
        bucket = os.getenv("TOS_BUCKET")
        if not endpoint or not region or not bucket:
            raise ProviderUnavailableError("TOS requires TOS_ENDPOINT, TOS_REGION, and TOS_BUCKET")
        digest, size = _hash_file(path)
        object_key = f"{self._config.object_prefix}/{digest.removeprefix('sha256:')}/{path.name}"
        client = self._client or self._make_client(endpoint, region)
        try:
            result = client.put_object_from_file(bucket, object_key, str(path))
        except Exception as exc:
            if _is_explicit_sdk_error(exc):
                raise ProviderError(f"TOS upload failed: {exc}") from exc
            raise DeliveryAmbiguousError(
                "TOS upload may have been delivered; automatic retry is forbidden"
            ) from exc
        version_id = getattr(result, "version_id", None)
        return MediaObjectRef(
            self.provider,
            bucket,
            object_key,
            digest,
            size,
            str(version_id) if version_id else None,
        )

    def presign_get(self, ref: MediaObjectRef) -> str:
        endpoint = os.getenv("TOS_ENDPOINT")
        region = os.getenv("TOS_REGION")
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
            )
        except Exception as exc:
            raise ProviderError(f"TOS presign failed: {exc}") from exc
        signed_url = getattr(result, "signed_url", None)
        if not isinstance(signed_url, str) or not signed_url.startswith("https://"):
            raise ContractError("TOS returned an invalid presigned HTTPS URL")
        return signed_url

    def close(self) -> None:
        close = getattr(self._client, "close", None)
        if callable(close):
            close()

    def _make_client(self, endpoint: str, region: str) -> Any:
        access_key = os.getenv("TOS_ACCESS_KEY")
        secret_key = os.getenv("TOS_SECRET_KEY")
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
    )


def _hash_file(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    try:
        with path.open("rb") as stream:
            while block := stream.read(1024 * 1024):
                digest.update(block)
                size += len(block)
    except OSError as exc:
        raise ContractError(f"media source is unreadable: {path}") from exc
    if size == 0:
        raise ContractError("media source is empty")
    return "sha256:" + digest.hexdigest(), size


def _is_explicit_sdk_error(exc: Exception) -> bool:
    module = getattr(type(exc), "__module__", "")
    return cast(bool, module.startswith("tos.exceptions") and getattr(exc, "status_code", None))
