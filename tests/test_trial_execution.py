from __future__ import annotations

from pathlib import Path

import pytest
from trial_helpers import admission, initialized_store, trial_config

from cueflow.errors import TrialExecutionStopped
from cueflow.orchestrator import _fail_run
from cueflow.project import RunContext
from cueflow.run_runtime import _new_invocation
from cueflow.trial_execution import TrialExecutionGate
from cueflow.trial_store import TrialStore


def test_interrupted_gate_prevents_new_registry_invocation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = trial_config(tmp_path)
    store = initialized_store(config)
    item = admission()
    # The gate and stale sweep must share the fixture's UTC timeline.
    monkeypatch.setattr("cueflow.trial_execution.now_utc", lambda: item.created_at)
    context = RunContext.create(tmp_path / "core", "trial")
    try:
        store.touch_visitor(
            item.visitor_id, fingerprint_hmac=None,
            now=item.created_at, online_window_seconds=300,
        )
        store.admit(item, config)
        store.bind_core_run(item.request_id, context.run_id)
        store.mark_running(item.request_id, now=item.created_at)
        store.close()

        gate = TrialExecutionGate(lambda: TrialStore(config.database_path), item.request_id)
        context.execution_control = gate
        _new_invocation(context, context.run_id, "media_upload", "tos", None, ())
        assert len(context.registry.invocations_for_run(context.run_id)) == 1
        counted = TrialStore(config.database_path)
        try:
            assert counted.request(item.request_id)["provider_invocation_count"] == 1
            assert counted.request(item.request_id)["last_alive_at"] == item.created_at
        finally:
            counted.close()

        sweep = TrialStore(config.database_path)
        try:
            assert sweep.sweep_stale(
                stale_before="2026-09-13T02:00:00.000000Z",
                now="2026-09-13T03:00:00.000000Z",
            ) == [item.request_id]
            assert sweep.request(item.request_id)["execution_status"] == "interrupted"
        finally:
            sweep.close()
        with pytest.raises(TrialExecutionStopped):
            _new_invocation(context, context.run_id, "qwen_asr", "qwen", "model", ())
        assert len(context.registry.invocations_for_run(context.run_id)) == 1
        counted = TrialStore(config.database_path)
        try:
            assert counted.request(item.request_id)["provider_invocation_count"] == 1
        finally:
            counted.close()
    finally:
        try:
            store.close()
        except Exception:
            pass
        context.close()


def test_gate_stop_is_core_interrupted_and_zero_cost_when_no_invocation_was_authorized(
    tmp_path: Path,
) -> None:
    config = trial_config(tmp_path)
    store = initialized_store(config)
    item = admission()
    context = RunContext.create(tmp_path / "core", "trial")
    try:
        store.touch_visitor(
            item.visitor_id, fingerprint_hmac=None,
            now=item.created_at, online_window_seconds=300,
        )
        store.admit(item, config)
        store.bind_core_run(item.request_id, context.run_id)
        store.mark_running(item.request_id, now=item.created_at)
        store.sweep_stale(
            stale_before="2026-09-13T02:00:00.000000Z",
            now="2026-09-13T03:00:00.000000Z",
        )
        _fail_run(context, context.run_id, TrialExecutionStopped("operator stopped"))
        store.mark_terminal(
            item.request_id,
            status="interrupted",
            now="2026-09-13T03:00:00.000000Z",
            reject_reason="operationally_stopped",
        )
        assert store.finalize_cost(
            item.request_id, now="2026-09-13T03:00:00.000000Z"
        ) == "not_incurred"
        request = store.request(item.request_id)
        assert context.registry.run(context.run_id)["status"] == "interrupted"
        assert request["provider_invocation_count"] == 0
        assert request["calculated_cost_micros"] == 0
        assert request["budget_released_at"] is not None
    finally:
        store.close()
        context.close()
