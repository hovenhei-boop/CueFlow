from __future__ import annotations

import os
import shutil
import tempfile
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from importlib import import_module
from pathlib import Path
from typing import Any, Protocol

from cueflow.config import TosConfig, TrialConfig
from cueflow.errors import ContractError, ProviderError, ProviderUnavailableError
from cueflow.media_object_store import MediaObjectRef, TosMediaObjectStore


@dataclass(frozen=True)
class DiskCapacity:
    used_ratio: float
    free_bytes: int
    required_free_bytes: int
    ready: bool


def check_disk_capacity(
    work_root: Path,
    *,
    global_concurrency: int,
    active_concurrency: int,
    max_source_bytes: int,
    expansion_factor: float,
    safety_margin_bytes: int,
    max_used_ratio: float,
) -> DiskCapacity:
    if global_concurrency <= 0 or active_concurrency < 0:
        raise ContractError("Trial concurrency settings must be non-negative")
    if expansion_factor < 1 or safety_margin_bytes < 0:
        raise ContractError("Trial disk safety settings are invalid")
    work_root.mkdir(parents=True, exist_ok=True)
    try:
        usage = shutil.disk_usage(work_root)
    except OSError as exc:
        raise ProviderUnavailableError("Trial workspace disk capacity is unavailable") from exc
    remaining = max(0, global_concurrency - active_concurrency)
    required = round(remaining * max_source_bytes * expansion_factor + safety_margin_bytes)
    used_ratio = usage.used / usage.total if usage.total else 1.0
    return DiskCapacity(
        used_ratio=used_ratio,
        free_bytes=usage.free,
        required_free_bytes=required,
        ready=used_ratio < max_used_ratio and usage.free >= required,
    )


@dataclass(frozen=True)
class LifecycleRule:
    prefix: str
    enabled: bool
    expiration_days: int | None
    has_unsupported_filter: bool = False
    has_date_expiration: bool = False


class LifecycleInspector(Protocol):
    def rules(self) -> Sequence[LifecycleRule]: ...

    def check_bucket_access(self) -> None: ...

    def check_object_access(self, prefix: str, url_ttl_seconds: int) -> None: ...


@dataclass(frozen=True)
class StorageReadiness:
    ready: bool
    reasons: tuple[str, ...]


def evaluate_storage_readiness(
    inspector: LifecycleInspector, config: TrialConfig
) -> StorageReadiness:
    reasons: list[str] = []
    prefixes = (config.source_prefix, config.work_prefix, config.result_prefix)
    overlaps = any(
        left != right and (_covers(left, right) or _covers(right, left))
        for left in prefixes for right in prefixes
    )
    if any(not _safe_prefix(value) for value in prefixes) or len(set(prefixes)) != 3 or overlaps:
        return StorageReadiness(
            False,
            ("Trial object prefixes must be distinct, relative, and non-overlapping",),
        )
    try:
        inspector.check_bucket_access()
        for prefix in prefixes:
            inspector.check_object_access(prefix, config.result_url_ttl_seconds)
        rules = tuple(inspector.rules())
    except (ContractError, ProviderError, ProviderUnavailableError, OSError) as exc:
        return StorageReadiness(False, (str(exc) or type(exc).__name__,))
    for prefix in (config.source_prefix, config.work_prefix):
        matching = [rule for rule in rules if _covers(rule.prefix, prefix) and rule.enabled]
        if not matching or any(
            rule.has_unsupported_filter
            or rule.expiration_days is None
            or rule.expiration_days > 7
            for rule in matching
        ):
            reasons.append(f"{prefix} requires an enabled expiration rule of at most 7 days")
    result_expiration = [
        rule for rule in rules
        if rule.enabled and _covers(rule.prefix, config.result_prefix)
        and (rule.expiration_days is not None or rule.has_date_expiration)
    ]
    if result_expiration:
        reasons.append(f"{config.result_prefix} must not be covered by an expiration rule")
    return StorageReadiness(not reasons, tuple(reasons))


class TosLifecycleInspector:
    """Read-only bucket/lifecycle probe; it never creates or edits lifecycle rules."""

    def __init__(
        self, *, client: Any | None = None, environment: Mapping[str, str] | None = None
    ) -> None:
        self._client = client
        self._environment = dict(os.environ if environment is None else environment)

    def check_bucket_access(self) -> None:
        client, bucket = self._ready()
        try:
            client.head_bucket(bucket)
        except Exception as exc:
            raise ProviderUnavailableError("Trial TOS bucket is not accessible") from exc

    def rules(self) -> Sequence[LifecycleRule]:
        client, bucket = self._ready()
        try:
            result = client.get_bucket_lifecycle(bucket)
        except Exception as exc:
            raise ProviderUnavailableError("Trial TOS lifecycle rules are not readable") from exc
        rules = getattr(result, "rules", None)
        if not isinstance(rules, Sequence):
            raise ProviderUnavailableError("Trial TOS lifecycle response has no rules")
        return tuple(_tos_rule(rule) for rule in rules)

    def check_object_access(self, prefix: str, url_ttl_seconds: int) -> None:
        client, _ = self._ready()
        temporary_path: Path | None = None
        store = TosMediaObjectStore(
            client=client,
            config=TosConfig(object_prefix=prefix, url_ttl_seconds=url_ttl_seconds),
            environment=self._environment,
        )
        ref: MediaObjectRef | None = None
        cleanup_error: Exception | None = None
        try:
            with tempfile.NamedTemporaryFile(
                prefix="cueflow-trial-readiness-", suffix=".txt", delete=False
            ) as stream:
                stream.write(b"cueflow-trial-readiness")
                temporary_path = Path(stream.name)
            ref = store.upload(
                temporary_path, object_name=f"readiness-{uuid.uuid4().hex}.txt"
            )
            if store.head(ref) is None:
                raise ProviderUnavailableError("Trial TOS readiness object was not persisted")
            store.presign_get(ref)
        except (ProviderError, ProviderUnavailableError, ContractError):
            raise
        except Exception as exc:
            raise ProviderUnavailableError("Trial TOS object permissions are incomplete") from exc
        finally:
            if ref is not None:
                try:
                    store.delete(ref)
                except Exception as exc:
                    cleanup_error = exc
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)
        if cleanup_error is not None:
            raise ProviderUnavailableError(
                "Trial TOS readiness object could not be removed"
            ) from cleanup_error

    def _ready(self) -> tuple[Any, str]:
        endpoint = self._environment.get("TOS_ENDPOINT")
        region = self._environment.get("TOS_REGION")
        bucket = self._environment.get("TOS_BUCKET")
        access_key = self._environment.get("TOS_ACCESS_KEY")
        secret_key = self._environment.get("TOS_SECRET_KEY")
        if not all((endpoint, region, bucket, access_key, secret_key)):
            raise ProviderUnavailableError("Trial TOS configuration is incomplete")
        if self._client is None:
            try:
                module = import_module("tos")
            except ImportError as exc:
                raise ProviderUnavailableError(
                    "Trial object storage requires the cueflow[cloud] dependencies"
                ) from exc
            self._client = module.TosClientV2(
                access_key, secret_key, endpoint, region, max_retry_count=0
            )
        return self._client, str(bucket)


def trial_store_factories(
    config: TrialConfig,
) -> tuple[
    Callable[[], TosMediaObjectStore],
    Callable[[], TosMediaObjectStore],
    Callable[[], TosMediaObjectStore],
]:
    def source() -> TosMediaObjectStore:
        return TosMediaObjectStore(config=TosConfig(
            object_prefix=config.source_prefix, url_ttl_seconds=7 * 24 * 60 * 60
        ))

    def work() -> TosMediaObjectStore:
        return TosMediaObjectStore(config=TosConfig(
            object_prefix=config.work_prefix, url_ttl_seconds=7 * 24 * 60 * 60
        ))

    def result() -> TosMediaObjectStore:
        return TosMediaObjectStore(config=TosConfig(
            object_prefix=config.result_prefix,
            url_ttl_seconds=config.result_url_ttl_seconds,
        ))

    return source, work, result


def object_ref_json(ref: MediaObjectRef) -> dict[str, Any]:
    return asdict(ref)


def media_ref_from_result(value: Mapping[str, Any]) -> MediaObjectRef:
    outputs = value.get("outputs")
    if not isinstance(outputs, Mapping) or not isinstance(outputs.get("srt"), Mapping):
        raise ContractError("successful Trial result is missing its persisted SRT receipt")
    raw = outputs["srt"]
    return MediaObjectRef(
        provider=str(raw["provider"]),
        bucket=str(raw["bucket"]),
        object_key=str(raw["object_key"]),
        content_hash=str(raw["content_hash"]),
        byte_length=int(raw["byte_length"]),
        version_id=str(raw["version_id"]) if raw.get("version_id") else None,
        mime_type=str(raw.get("mime_type", "application/x-subrip")),
    )


def safe_job_workspace(work_root: Path, job_id: str) -> Path:
    if not job_id.startswith("job_") or len(job_id) != 36:
        raise ContractError("invalid Trial job identity")
    if any(character not in "0123456789abcdef" for character in job_id[4:]):
        raise ContractError("invalid Trial job identity")
    root = work_root.resolve()
    result = (root / job_id).resolve()
    if result.parent != root:
        raise ContractError("Trial workspace escaped its configured root")
    return result


def _safe_prefix(value: str) -> bool:
    return bool(value and not value.startswith("/") and ".." not in value.split("/"))


def _covers(rule_prefix: str, object_prefix: str) -> bool:
    normalized_rule = rule_prefix.rstrip("/")
    normalized_object = object_prefix.rstrip("/")
    return not normalized_rule or (
        normalized_object == normalized_rule or normalized_object.startswith(normalized_rule + "/")
    )


def _tos_rule(value: Any) -> LifecycleRule:
    raw_status = getattr(value, "status", "")
    status = str(getattr(raw_status, "value", raw_status)).lower()
    prefix = getattr(value, "prefix", None)
    filter_value = getattr(value, "filter", None)
    unsupported = filter_value is not None or bool(getattr(value, "tags", None))
    expiration = getattr(value, "expiration", None)
    days = getattr(expiration, "days", None) if expiration is not None else None
    return LifecycleRule(
        prefix=str(prefix or ""),
        enabled=status in {"enabled", "status_enable", "1", "true"},
        expiration_days=int(days) if days is not None else None,
        has_unsupported_filter=unsupported,
        has_date_expiration=bool(getattr(expiration, "date", None)),
    )
