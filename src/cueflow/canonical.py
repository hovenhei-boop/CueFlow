from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from typing import Any

import rfc8785

from cueflow.errors import ContractError


def canonical_bytes(value: Any) -> bytes:
    try:
        return rfc8785.dumps(value)
    except (TypeError, ValueError) as exc:
        raise ContractError(f"value is not valid RFC 8785/I-JSON data: {exc}") from exc


def sha256_prefixed(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def hash_json(value: Any) -> str:
    return sha256_prefixed(canonical_bytes(value))


def artifact_hash_projection(
    *,
    artifact_kind: str,
    scope_key: str,
    schema_version: str,
    producer: Mapping[str, Any],
    inputs: Sequence[Mapping[str, Any]],
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    parts = schema_version.split(".")
    if len(parts) != 3 or not all(part.isdigit() for part in parts):
        raise ContractError(f"invalid schema_version: {schema_version}")
    return {
        "artifact_kind": artifact_kind,
        "scope_key": scope_key,
        "schema_semantics": {"major": int(parts[0]), "minor": int(parts[1])},
        "producer": dict(producer),
        "inputs": [dict(item) for item in inputs],
        "payload": dict(payload),
    }


def artifact_content_hash(
    *,
    artifact_kind: str,
    scope_key: str,
    schema_version: str,
    producer: Mapping[str, Any],
    inputs: Sequence[Mapping[str, Any]],
    payload: Mapping[str, Any],
) -> str:
    return hash_json(
        artifact_hash_projection(
            artifact_kind=artifact_kind,
            scope_key=scope_key,
            schema_version=schema_version,
            producer=producer,
            inputs=inputs,
            payload=payload,
        )
    )
