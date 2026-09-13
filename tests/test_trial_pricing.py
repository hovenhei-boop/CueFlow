from __future__ import annotations

from pathlib import Path

from cueflow.trial_pricing import OPERATION_COST_CLASS, TrialPricing

PRICING = Path(__file__).parent / "fixtures" / "trial_pricing.json"


def test_every_registry_operation_has_an_explicit_cost_policy() -> None:
    registry_sql = (Path(__file__).parents[1] / "src" / "cueflow" / "registry.py").read_text()
    operations = {
        "media_upload", "qwen_asr", "doubao_asr", "glm_selection",
        "qwen_correction", "kimi_correction", "ata",
    }
    assert set(OPERATION_COST_CLASS) == operations
    assert all(f"'{operation}'" in registry_sql for operation in operations)


def test_token_and_duration_costs_use_actual_units_and_version() -> None:
    pricing = TrialPricing(PRICING)
    token = pricing.cost(
        operation="qwen_asr",
        provider="dashscope",
        resolved_model="qwen-test",
        invocation_status="succeeded",
        usage={"input_tokens": 1000, "output_tokens": 100, "cached_tokens": 400},
        audio_duration_ms=60_000,
        at="2026-09-13T00:00:00Z",
    )
    assert token.calculated_cost_micros == 880
    assert token.usage_source == "provider_actual"
    assert token.pricing_version == "test-2026-09-01"

    duration = pricing.cost(
        operation="doubao_asr",
        provider="volcengine",
        resolved_model="bigmodel",
        invocation_status="succeeded",
        usage=None,
        audio_duration_ms=90_000,
        at="2026-09-13T00:00:00Z",
    )
    assert duration.calculated_cost_micros == 180_000
    assert duration.usage_source == "duration_derived"


def test_missing_usage_is_unknown_not_zero_and_not_sent_is_zero() -> None:
    pricing = TrialPricing(PRICING)
    unknown = pricing.cost(
        operation="qwen_asr", provider="dashscope", resolved_model="qwen-test",
        invocation_status="explicit_failure", usage=None, audio_duration_ms=1,
        at="2026-09-13T00:00:00Z",
    )
    assert unknown.cost_status == "unknown"
    assert unknown.calculated_cost_micros is None
    not_sent = pricing.cost(
        operation="qwen_asr", provider="dashscope", resolved_model="qwen-test",
        invocation_status="definitely_not_sent", usage=None, audio_duration_ms=1,
        at="2026-09-13T00:00:00Z",
    )
    assert not_sent.cost_status == "not_incurred"
    assert not_sent.calculated_cost_micros == 0
