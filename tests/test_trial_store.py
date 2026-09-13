from __future__ import annotations

from pathlib import Path

import pytest
from trial_helpers import admission, initialized_store, trial_config

from cueflow.errors import TrialAdmissionError
from cueflow.trial_store import before

NOW = "2026-09-13T01:00:00.000000Z"


def test_stale_releases_execution_but_not_budget_then_unknown_reclassifies(tmp_path: Path) -> None:
    config = trial_config(tmp_path)
    store = initialized_store(config)
    try:
        item = admission()
        store.touch_visitor(
            item.visitor_id, fingerprint_hmac=item.fingerprint_hmac,
            now=NOW, online_window_seconds=300,
        )
        store.admit(item, config)
        store.bind_core_run(item.request_id, "run_test")
        store.mark_running(item.request_id, now=NOW)

        assert store.sweep_stale(
            stale_before="2026-09-13T02:00:00.000000Z",
            now="2026-09-13T03:00:00.000000Z",
        ) == [item.request_id]
        interrupted = store.request(item.request_id)
        assert interrupted["execution_status"] == "interrupted"
        assert interrupted["budget_released_at"] is None

        assert store.classify_unknown(
            created_before="2026-09-14T02:00:00.000000Z",
            now="2026-09-14T03:00:00.000000Z",
        ) == [item.request_id]
        unknown = store.request(item.request_id)
        assert unknown["cost_status"] == "unknown"
        assert unknown["estimated_unknown_cost_micros"] == 1_000_000
        assert unknown["calculated_cost_micros"] is None
        assert unknown["budget_released_at"] == "2026-09-14T03:00:00.000000Z"
    finally:
        store.close()


def test_peak_online_is_stats_not_control_and_operator_changes_are_append_only(
    tmp_path: Path,
) -> None:
    config = trial_config(tmp_path)
    store = initialized_store(config)
    try:
        for value in ("1", "2"):
            store.touch_visitor(
                "v_" + value * 32,
                fingerprint_hmac=None,
                now=NOW,
                online_window_seconds=300,
            )
        assert store.connection.execute("SELECT peak_online FROM trial_stats").fetchone()[0] == 2
        assert store.connection.execute("SELECT COUNT(*) FROM trial_control").fetchone()[0] == 0

        changed = store.append_control(
            "set_daily_budget_override",
            reason="temporary experiment cap",
            daily_budget_micros=2_000_000,
            default_daily_budget_micros=config.daily_budget_micros,
            now=NOW,
        )
        assert changed["daily_budget_micros"] == 2_000_000
        audit = store.connection.execute("SELECT * FROM trial_control").fetchone()
        assert audit["reason"] == "temporary experiment cap"
        assert "daily_budget_micros" in audit["old_value_json"]
        assert "2000000" in audit["new_value_json"]
    finally:
        store.close()


def test_atomic_admission_enforces_concurrency_and_records_rejection(tmp_path: Path) -> None:
    config = trial_config(tmp_path, visitor_concurrency=1)
    store = initialized_store(config)
    try:
        first = admission()
        store.touch_visitor(
            first.visitor_id, fingerprint_hmac=None, now=NOW, online_window_seconds=300
        )
        store.admit(first, config)
        second = admission(
            request_id="req_" + "2" * 32,
            job_id="job_" + "2" * 32,
            created_at="2026-09-13T01:01:00.000000Z",
        )
        with pytest.raises(TrialAdmissionError) as raised:
            store.admit(second, config)
        assert raised.value.reason == "visitor_concurrency"
        assert store.request(second.request_id)["execution_status"] == "rejected"
        assert store.request(second.request_id)["budget_released_at"] is not None
    finally:
        store.close()


def test_success_rate_excludes_interrupted_rejected_and_cancelled(tmp_path: Path) -> None:
    config = trial_config(tmp_path, visitor_concurrency=10, ip_concurrency=10)
    store = initialized_store(config)
    try:
        visitor = "v_" + "1" * 32
        store.touch_visitor(visitor, fingerprint_hmac=None, now=NOW, online_window_seconds=300)
        for index, status in enumerate(("succeeded", "failed", "interrupted", "cancelled"), 1):
            item = admission(
                request_id="req_" + str(index) * 32,
                job_id="job_" + str(index) * 32,
                created_at=f"2026-09-13T01:0{index}:00.000000Z",
            )
            store.admit(item, config)
            store.mark_running(item.request_id, now=item.created_at)
            store.mark_terminal(item.request_id, status=status, now=item.created_at)
            store.finalize_cost(item.request_id, now=item.created_at)
        summary = store.summary(
            accounting_date="2026-09-13",
            online_after=before(NOW, 300),
            default_daily_budget_micros=config.daily_budget_micros,
        )
        assert summary["quality_denominator"] == 2
        assert summary["success_rate"] == 0.5
        assert summary["failure_rate"] == 0.5
        assert summary["interrupted"] == 1
    finally:
        store.close()


def test_interrupted_request_cannot_be_resurrected_by_late_completion(tmp_path: Path) -> None:
    config = trial_config(tmp_path)
    store = initialized_store(config)
    try:
        item = admission()
        store.touch_visitor(
            item.visitor_id, fingerprint_hmac=None, now=NOW, online_window_seconds=300
        )
        store.admit(item, config)
        store.mark_running(item.request_id, now=NOW)
        store.mark_terminal(
            item.request_id,
            status="interrupted",
            now="2026-09-13T02:00:00.000000Z",
            reject_reason="stale_worker",
        )

        store.mark_terminal(
            item.request_id,
            status="succeeded",
            now="2026-09-13T02:01:00.000000Z",
        )

        row = store.request(item.request_id)
        assert row["execution_status"] == "interrupted"
        assert row["reject_reason"] == "stale_worker"
    finally:
        store.close()


def test_success_with_missing_usage_becomes_unknown_after_24_hours(tmp_path: Path) -> None:
    config = trial_config(tmp_path)
    store = initialized_store(config)
    try:
        item = admission()
        store.touch_visitor(
            item.visitor_id, fingerprint_hmac=None, now=NOW, online_window_seconds=300
        )
        store.admit(item, config)
        store.mark_running(item.request_id, now=NOW)
        assert store.authorize_invocation(item.request_id, "run_test", now=NOW) is False
        store.bind_core_run(item.request_id, "run_test")
        assert store.authorize_invocation(item.request_id, "run_test", now=NOW) is True
        store.mark_terminal(item.request_id, status="succeeded", now=NOW)

        assert store.classify_unknown(
            created_before="2026-09-14T02:00:00.000000Z",
            now="2026-09-14T03:00:00.000000Z",
        ) == [item.request_id]
        row = store.request(item.request_id)
        assert row["cost_status"] == "unknown"
        assert row["estimated_unknown_cost_micros"] == item.estimated_max_cost_micros
        assert row["budget_released_at"] == "2026-09-14T03:00:00.000000Z"
    finally:
        store.close()


def test_today_uv_includes_heartbeat_only_visitors_in_china_reporting_day(
    tmp_path: Path,
) -> None:
    config = trial_config(tmp_path)
    store = initialized_store(config)
    try:
        store.touch_visitor(
            "v_" + "8" * 32,
            fingerprint_hmac=None,
            now="2026-09-12T16:01:00.000000Z",
            online_window_seconds=300,
        )
        store.touch_visitor(
            "v_" + "9" * 32,
            fingerprint_hmac=None,
            now="2026-09-12T15:59:00.000000Z",
            online_window_seconds=300,
        )

        summary = store.summary(
            accounting_date="2026-09-13",
            online_after="2026-09-12T16:00:00.000000Z",
            default_daily_budget_micros=config.daily_budget_micros,
        )

        assert summary["today_cookie_uv"] == 1
        assert summary["new_visitors_today"] == 1
        assert summary["returning_visitors_today"] == 0
    finally:
        store.close()
