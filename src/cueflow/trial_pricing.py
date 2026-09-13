from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from cueflow.errors import ContractError

OPERATION_COST_CLASS: dict[str, str] = {
    "media_upload": "non_billable_api",
    "qwen_asr": "provider_or_duration",
    "doubao_asr": "duration_derived",
    "glm_selection": "provider_actual",
    "qwen_correction": "provider_actual",
    "kimi_correction": "provider_actual",
    "ata": "duration_derived",
}


@dataclass(frozen=True)
class UsageCost:
    usage_source: str
    pricing_version: str | None
    calculated_cost_micros: int | None
    currency: str
    cost_status: str
    input_tokens: int | None = None
    output_tokens: int | None = None
    cached_tokens: int | None = None
    reasoning_tokens: int | None = None
    total_tokens: int | None = None


class TrialPricing:
    """Versioned, append-only pricing snapshot selected by Provider invocation time."""

    def __init__(self, path: Path) -> None:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ContractError(f"Trial pricing file is unreadable: {path}") from exc
        if not isinstance(value, Mapping):
            raise ContractError("Trial pricing must be a JSON object")
        versions = value.get("versions")
        if not isinstance(versions, list) or not versions:
            raise ContractError("Trial pricing requires a non-empty versions list")
        self._versions: list[dict[str, Any]] = []
        for raw in versions:
            if not isinstance(raw, Mapping):
                raise ContractError("Trial pricing version must be an object")
            version = dict(raw)
            if not isinstance(version.get("pricing_version"), str):
                raise ContractError("Trial pricing_version is required")
            if not isinstance(version.get("effective_at"), str):
                raise ContractError("Trial pricing effective_at is required")
            if version.get("currency") != "CNY":
                raise ContractError("Trial pricing currency must be CNY")
            if not isinstance(version.get("estimate_micros_per_audio_minute"), int):
                raise ContractError("Trial pricing requires an integer maximum estimate rate")
            rules = version.get("rules")
            if not isinstance(rules, list):
                raise ContractError("Trial pricing rules must be a list")
            self._validate_rules(rules)
            self._versions.append(version)
        self._versions.sort(key=lambda item: _timestamp(str(item["effective_at"])))

    def estimate_max_cost(self, audio_duration_ms: int, *, at: str) -> int:
        version = self._version(at)
        minutes = max(1.0, audio_duration_ms / 60_000)
        rate = int(version["estimate_micros_per_audio_minute"])
        return max(0, round(minutes * rate))

    def cost(
        self,
        *,
        operation: str,
        provider: str,
        resolved_model: str | None,
        invocation_status: str,
        usage: Mapping[str, Any] | None,
        audio_duration_ms: int,
        at: str,
    ) -> UsageCost:
        classification = OPERATION_COST_CLASS.get(operation)
        if classification is None:
            raise ContractError(f"Trial pricing has no operation policy for {operation}")
        if invocation_status == "definitely_not_sent":
            return UsageCost("not_incurred", None, 0, "CNY", "not_incurred")
        if classification == "non_billable_api":
            return UsageCost("non_billable_api", None, 0, "CNY", "not_incurred")
        version = self._version(at)
        rule = self._rule(version, operation, provider, resolved_model)
        if rule is None:
            return UsageCost("missing_pricing_rule", None, None, "CNY", "unknown")
        billing_unit = str(rule["billing_unit"])
        if billing_unit == "audio_minute":
            cost = round(audio_duration_ms / 60_000 * int(rule["rates"]["micros_per_minute"]))
            return UsageCost(
                "duration_derived", str(version["pricing_version"]), cost, "CNY", "calculated"
            )
        normalized = _normalized_usage(usage)
        if normalized is None:
            return UsageCost(
                "provider_usage_missing",
                str(version["pricing_version"]),
                None,
                "CNY",
                "unknown" if invocation_status != "sending" else "pending",
            )
        rates = rule["rates"]
        normal_input = max(0, normalized["input_tokens"] - normalized["cached_tokens"])
        token_cost = (
            normal_input * int(rates["input_micros_per_million"])
            + normalized["cached_tokens"] * int(rates.get(
                "cached_input_micros_per_million", rates["input_micros_per_million"]
            ))
            + normalized["output_tokens"] * int(rates["output_micros_per_million"])
        ) / 1_000_000
        return UsageCost(
            "provider_actual",
            str(version["pricing_version"]),
            round(token_cost),
            "CNY",
            "calculated",
            **normalized,
        )

    def _version(self, at: str) -> dict[str, Any]:
        target = _timestamp(at)
        candidates = [
            version
            for version in self._versions
            if _timestamp(str(version["effective_at"])) <= target
        ]
        if not candidates:
            raise ContractError("no Trial pricing version was effective at invocation time")
        return candidates[-1]

    @staticmethod
    def _rule(
        version: Mapping[str, Any], operation: str, provider: str, resolved_model: str | None
    ) -> Mapping[str, Any] | None:
        for raw in version["rules"]:
            rule = dict(raw)
            if rule["operation"] != operation or rule["provider"] != provider:
                continue
            configured_model = rule.get("resolved_model")
            if configured_model in {None, "*"} or configured_model == resolved_model:
                return rule
        return None

    @staticmethod
    def _validate_rules(rules: list[Any]) -> None:
        for raw in rules:
            if not isinstance(raw, Mapping):
                raise ContractError("Trial pricing rule must be an object")
            operation = raw.get("operation")
            if operation not in OPERATION_COST_CLASS:
                raise ContractError(f"Trial pricing rule has unknown operation: {operation}")
            if not isinstance(raw.get("provider"), str):
                raise ContractError("Trial pricing rule provider is required")
            unit = raw.get("billing_unit")
            rates = raw.get("rates")
            if unit not in {"tokens", "audio_minute"} or not isinstance(rates, Mapping):
                raise ContractError("Trial pricing rule has an unsupported billing unit")
            needed = (
                {"input_micros_per_million", "output_micros_per_million"}
                if unit == "tokens"
                else {"micros_per_minute"}
            )
            if not needed.issubset(rates) or any(
                not isinstance(rates[key], int) or int(rates[key]) < 0 for key in needed
            ):
                raise ContractError("Trial pricing rates must be non-negative integer micros")


def usage_record(
    invocation: Mapping[str, Any],
    *,
    request_id: str,
    audio_duration_ms: int,
    pricing: TrialPricing,
    now: str,
) -> dict[str, Any]:
    usage = _read_usage(invocation.get("usage_json"))
    cost = pricing.cost(
        operation=str(invocation["operation"]),
        provider=str(invocation["provider"]),
        resolved_model=(
            str(invocation["resolved_model"] or invocation["requested_model"])
            if invocation.get("resolved_model") or invocation.get("requested_model")
            else None
        ),
        invocation_status=str(invocation["status"]),
        usage=usage,
        audio_duration_ms=audio_duration_ms,
        at=str(invocation["created_at"]),
    )
    return {
        "invocation_id": str(invocation["invocation_id"]),
        "request_id": request_id,
        "operation": str(invocation["operation"]),
        "provider": str(invocation["provider"]),
        "resolved_model": invocation.get("resolved_model") or invocation.get("requested_model"),
        "invocation_status": str(invocation["status"]),
        "input_tokens": cost.input_tokens,
        "output_tokens": cost.output_tokens,
        "cached_tokens": cost.cached_tokens,
        "reasoning_tokens": cost.reasoning_tokens,
        "total_tokens": cost.total_tokens,
        "usage_json": (
            json.dumps(usage, sort_keys=True, separators=(",", ":")) if usage is not None else None
        ),
        "usage_source": cost.usage_source,
        "pricing_version": cost.pricing_version,
        "calculated_cost_micros": cost.calculated_cost_micros,
        "currency": cost.currency,
        "cost_status": cost.cost_status,
        "created_at": str(invocation["created_at"]),
        "updated_at": now,
    }


def _normalized_usage(usage: Mapping[str, Any] | None) -> dict[str, int] | None:
    if usage is None:
        return None
    input_tokens = _integer(usage, "input_tokens", "prompt_tokens")
    output_tokens = _integer(usage, "output_tokens", "completion_tokens")
    if input_tokens is None or output_tokens is None:
        return None
    details = usage.get("prompt_tokens_details")
    cached = _integer(usage, "cached_tokens")
    if cached is None and isinstance(details, Mapping):
        cached = _integer(details, "cached_tokens")
    completion_details = usage.get("completion_tokens_details")
    reasoning = _integer(usage, "reasoning_tokens")
    if reasoning is None and isinstance(completion_details, Mapping):
        reasoning = _integer(completion_details, "reasoning_tokens")
    total = _integer(usage, "total_tokens")
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cached_tokens": cached or 0,
        "reasoning_tokens": reasoning or 0,
        "total_tokens": total if total is not None else input_tokens + output_tokens,
    }


def _read_usage(value: object) -> Mapping[str, Any] | None:
    if value is None:
        return None
    if isinstance(value, Mapping):
        return value
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError:
            return None
        return decoded if isinstance(decoded, Mapping) else None
    return None


def _integer(value: Mapping[str, Any], *keys: str) -> int | None:
    for key in keys:
        item = value.get(key)
        if isinstance(item, int) and item >= 0:
            return item
    return None


def _timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))
