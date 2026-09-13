from __future__ import annotations

import json
import math
import sqlite3
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import median
from typing import Any, cast

from cueflow.config import TrialConfig
from cueflow.errors import ContractError, TrialAdmissionError, TrialNotFoundError, TrialStoreError
from cueflow.trial_migrations import validate_trial_schema

_ACCEPTED_STATUSES = (
    "queued", "running", "needs_review", "succeeded", "failed", "cancelled", "interrupted"
)
_CONCURRENCY_STATUSES = ("queued", "running")


@dataclass(frozen=True)
class TrialAdmission:
    request_id: str
    job_id: str
    visitor_id: str
    action_kind: str
    ip_hmac: str
    fingerprint_hmac: str | None
    audio_duration_ms: int
    estimated_max_cost_micros: int
    created_at: str
    accounting_date: str


class TrialStore:
    """Short-lived connection to the independent anonymous Trial control plane."""

    def __init__(self, path: Path) -> None:
        if not path.is_absolute():
            raise TrialStoreError("Trial database path must be absolute")
        if not path.is_file():
            raise TrialStoreError("Trial database must be initialized before it is opened")
        self.path = path
        self.connection = sqlite3.connect(path, timeout=5.0)
        self.connection.row_factory = sqlite3.Row
        try:
            self.connection.execute("PRAGMA foreign_keys=ON")
            validate_trial_schema(self.connection)
            self.connection.execute("PRAGMA journal_mode=WAL")
            self.connection.execute("PRAGMA busy_timeout=5000")
        except BaseException:
            self.connection.close()
            raise

    def close(self) -> None:
        self.connection.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        if self.connection.in_transaction:
            raise ContractError("nested TrialStore transactions are not supported")
        try:
            self.connection.execute("BEGIN IMMEDIATE")
            yield self.connection
        except BaseException:
            self.connection.rollback()
            raise
        else:
            self.connection.commit()

    def touch_visitor(
        self,
        visitor_id: str,
        *,
        fingerprint_hmac: str | None,
        now: str,
        online_window_seconds: int,
    ) -> dict[str, Any]:
        with self.transaction() as tx:
            row = tx.execute(
                "SELECT * FROM trial_visitors WHERE visitor_id=?", (visitor_id,)
            ).fetchone()
            if row is None:
                tx.execute(
                    """INSERT INTO trial_visitors(
                           visitor_id, fingerprint_hmac, fingerprint_observed_at,
                           first_seen_at, last_seen_at
                       ) VALUES (?, ?, ?, ?, ?)""",
                    (visitor_id, fingerprint_hmac, now if fingerprint_hmac else None, now, now),
                )
            else:
                tx.execute(
                    """UPDATE trial_visitors
                       SET last_seen_at=?,
                           fingerprint_hmac=COALESCE(?, fingerprint_hmac),
                           fingerprint_observed_at=CASE WHEN ? IS NULL
                               THEN fingerprint_observed_at ELSE ? END
                       WHERE visitor_id=?""",
                    (now, fingerprint_hmac, fingerprint_hmac, now, visitor_id),
                )
            threshold = _iso(_parse(now) - timedelta(seconds=online_window_seconds))
            online = int(tx.execute(
                "SELECT COUNT(*) FROM trial_visitors WHERE last_seen_at>=?", (threshold,)
            ).fetchone()[0])
            stats = tx.execute(
                "SELECT peak_online FROM trial_stats WHERE stats_key='global'"
            ).fetchone()
            if stats is None:
                raise TrialStoreError("Trial statistics row is missing")
            if online > int(stats[0]):
                tx.execute(
                    """UPDATE trial_stats SET peak_online=?, peak_online_at=?, updated_at=?
                       WHERE stats_key='global'""",
                    (online, now, now),
                )
        return {"visitor_id": visitor_id, "online_now": online}

    def admit(self, admission: TrialAdmission, config: TrialConfig) -> None:
        rejected: tuple[str, int] | None = None
        with self.transaction() as tx:
            visitor = tx.execute(
                "SELECT * FROM trial_visitors WHERE visitor_id=?", (admission.visitor_id,)
            ).fetchone()
            if visitor is None:
                raise ContractError("visitor must be registered before admission")
            rejection = self._admission_rejection(tx, admission, config, visitor)
            if rejection is not None:
                reason, status_code = rejection
                self._insert_request(tx, admission, "rejected", reason, admission.created_at)
                rejected = reason, status_code
            else:
                self._insert_request(tx, admission, "queued", None, None)
                previous_date = visitor["last_active_date"]
                active_days = int(visitor["active_day_count"])
                if previous_date != admission.accounting_date:
                    active_days += 1
                second_at = visitor["second_active_day_at"]
                if active_days >= 2 and second_at is None:
                    second_at = admission.created_at
                tx.execute(
                    """UPDATE trial_visitors
                       SET first_job_at=COALESCE(first_job_at, ?), second_active_day_at=?,
                           active_day_count=?, last_active_date=?, last_seen_at=?
                       WHERE visitor_id=?""",
                    (
                        admission.created_at,
                        second_at,
                        active_days,
                        admission.accounting_date,
                        admission.created_at,
                        admission.visitor_id,
                    ),
                )
        if rejected is not None:
            reason, status_code = rejected
            raise TrialAdmissionError(
                _admission_message(reason), reason=reason, status_code=status_code
            )

    def record_external_rejection(
        self, admission: TrialAdmission, *, reason: str, status_code: int
    ) -> TrialAdmissionError:
        with self.transaction() as tx:
            exists = tx.execute(
                "SELECT 1 FROM trial_visitors WHERE visitor_id=?", (admission.visitor_id,)
            ).fetchone()
            if exists is None:
                raise ContractError("visitor must be registered before rejection")
            self._insert_request(tx, admission, "rejected", reason, admission.created_at)
        return TrialAdmissionError(
            _admission_message(reason), reason=reason, status_code=status_code
        )

    def _insert_request(
        self,
        tx: sqlite3.Connection,
        admission: TrialAdmission,
        status: str,
        reject_reason: str | None,
        budget_released_at: str | None,
    ) -> None:
        tx.execute(
            """INSERT INTO trial_requests(
                   request_id, job_id, visitor_id, action_kind, accounting_date,
                   ip_hmac, fingerprint_hmac, execution_status, reject_reason, created_at,
                   audio_duration_ms, estimated_max_cost_micros, cost_status,
                   calculated_cost_micros, budget_released_at
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                admission.request_id,
                admission.job_id,
                admission.visitor_id,
                admission.action_kind,
                admission.accounting_date,
                admission.ip_hmac,
                admission.fingerprint_hmac,
                status,
                reject_reason,
                admission.created_at,
                admission.audio_duration_ms,
                admission.estimated_max_cost_micros,
                "not_incurred" if status == "rejected" else "pending",
                0 if status == "rejected" else None,
                budget_released_at,
            ),
        )

    def _admission_rejection(
        self,
        tx: sqlite3.Connection,
        admission: TrialAdmission,
        config: TrialConfig,
        visitor: sqlite3.Row,
    ) -> tuple[str, int] | None:
        if visitor["status"] == "blocked":
            return "visitor_blocked", 429
        control = self._current_control(tx, config.daily_budget_micros)
        if control["paused"]:
            return "trial_paused", 503
        hour_start = _iso(_parse(admission.created_at) - timedelta(hours=1))
        accepted = "('" + "','".join(_ACCEPTED_STATUSES) + "')"
        visitor_hour = int(tx.execute(
            f"""SELECT COUNT(*) FROM trial_requests
                WHERE visitor_id=? AND created_at>=? AND execution_status IN {accepted}""",
            (admission.visitor_id, hour_start),
        ).fetchone()[0])
        visitor_day = int(tx.execute(
            f"""SELECT COUNT(*) FROM trial_requests
                WHERE visitor_id=? AND accounting_date=? AND execution_status IN {accepted}""",
            (admission.visitor_id, admission.accounting_date),
        ).fetchone()[0])
        ip_hour = int(tx.execute(
            f"""SELECT COUNT(*) FROM trial_requests
                WHERE ip_hmac=? AND created_at>=? AND execution_status IN {accepted}""",
            (admission.ip_hmac, hour_start),
        ).fetchone()[0])
        ip_day = int(tx.execute(
            f"""SELECT COUNT(*) FROM trial_requests
                WHERE ip_hmac=? AND accounting_date=? AND execution_status IN {accepted}""",
            (admission.ip_hmac, admission.accounting_date),
        ).fetchone()[0])
        audio_day = int(tx.execute(
            f"""SELECT COALESCE(SUM(audio_duration_ms), 0) FROM trial_requests
                WHERE visitor_id=? AND accounting_date=? AND execution_status IN {accepted}""",
            (admission.visitor_id, admission.accounting_date),
        ).fetchone()[0])
        if visitor_hour >= config.visitor_hourly_jobs:
            return "visitor_hourly_quota", 429
        if visitor_day >= config.visitor_daily_jobs:
            return "visitor_daily_quota", 429
        if audio_day + admission.audio_duration_ms > config.visitor_daily_audio_ms:
            return "visitor_daily_audio_quota", 429
        if ip_hour >= config.ip_hourly_jobs:
            return "ip_hourly_quota", 429
        if ip_day >= config.ip_daily_jobs:
            return "ip_daily_quota", 429
        concurrency = "('" + "','".join(_CONCURRENCY_STATUSES) + "')"
        visitor_running = int(tx.execute(
            f"SELECT COUNT(*) FROM trial_requests WHERE visitor_id=? "
            f"AND execution_status IN {concurrency}",
            (admission.visitor_id,),
        ).fetchone()[0])
        ip_running = int(tx.execute(
            f"SELECT COUNT(*) FROM trial_requests WHERE ip_hmac=? "
            f"AND execution_status IN {concurrency}",
            (admission.ip_hmac,),
        ).fetchone()[0])
        global_running = int(tx.execute(
            f"SELECT COUNT(*) FROM trial_requests WHERE execution_status IN {concurrency}"
        ).fetchone()[0])
        if visitor_running >= config.visitor_concurrency:
            return "visitor_concurrency", 429
        if ip_running >= config.ip_concurrency:
            return "ip_concurrency", 429
        if global_running >= config.global_concurrency:
            return "global_concurrency", 503
        costs = tx.execute(
            """SELECT
                   COALESCE(SUM(calculated_cost_micros), 0),
                   COALESCE(SUM(CASE WHEN budget_released_at IS NULL
                                    THEN estimated_max_cost_micros ELSE 0 END), 0),
                   COALESCE(SUM(estimated_unknown_cost_micros), 0)
               FROM trial_requests WHERE accounting_date=?""",
            (admission.accounting_date,),
        ).fetchone()
        projected = int(costs[0]) + int(costs[1]) + int(costs[2])
        if projected + admission.estimated_max_cost_micros > int(control["daily_budget_micros"]):
            return "daily_budget", 503
        return None

    def _current_control(
        self, tx: sqlite3.Connection, default_daily_budget_micros: int
    ) -> dict[str, Any]:
        paused = False
        override: int | None = None
        rows = tx.execute(
            "SELECT action, new_value_json FROM trial_control ORDER BY control_revision"
        ).fetchall()
        for row in rows:
            action = str(row["action"])
            value = json.loads(str(row["new_value_json"]))
            if action == "pause":
                paused = True
            elif action == "resume":
                paused = False
            elif action == "set_daily_budget_override":
                override = int(value["daily_budget_micros"])
            elif action == "clear_daily_budget_override":
                override = None
        return {
            "paused": paused,
            "daily_budget_override_micros": override,
            "daily_budget_micros": override or default_daily_budget_micros,
        }

    def current_control(self, default_daily_budget_micros: int) -> dict[str, Any]:
        return self._current_control(self.connection, default_daily_budget_micros)

    def append_control(
        self,
        action: str,
        *,
        reason: str,
        default_daily_budget_micros: int,
        daily_budget_micros: int | None = None,
        now: str,
    ) -> dict[str, Any]:
        if action not in {
            "pause", "resume", "set_daily_budget_override", "clear_daily_budget_override"
        }:
            raise ContractError("invalid Trial control action")
        if not reason.strip():
            raise ContractError("Trial control reason must not be empty")
        if action == "set_daily_budget_override" and (
            daily_budget_micros is None or daily_budget_micros <= 0
        ):
            raise ContractError("daily budget override must be positive")
        with self.transaction() as tx:
            old = self._current_control(tx, default_daily_budget_micros)
            if action == "set_daily_budget_override":
                new_value: Mapping[str, Any] = {
                    "daily_budget_micros": daily_budget_micros
                }
            else:
                new_value = {}
            cursor = tx.execute(
                """INSERT INTO trial_control(
                       action, old_value_json, new_value_json, reason, created_at
                   ) VALUES (?, ?, ?, ?, ?)""",
                (
                    action,
                    json.dumps(old, sort_keys=True, separators=(",", ":")),
                    json.dumps(new_value, sort_keys=True, separators=(",", ":")),
                    reason.strip(),
                    now,
                ),
            )
            if cursor.lastrowid is None:
                raise TrialStoreError("Trial control revision was not created")
            revision = int(cursor.lastrowid)
            current = self._current_control(tx, default_daily_budget_micros)
        return {"control_revision": revision, **current}

    def bind_core_run(self, request_id: str, core_run_id: str) -> None:
        self._update_one(
            "UPDATE trial_requests SET core_run_id=? WHERE request_id=? AND core_run_id IS NULL",
            (core_run_id, request_id),
        )

    def record_source_object(self, request_id: str, value: Mapping[str, Any]) -> None:
        encoded = json.dumps(dict(value), sort_keys=True, separators=(",", ":"))
        self._update_one(
            "UPDATE trial_requests SET source_object_json=? WHERE request_id=?",
            (encoded, request_id),
        )

    def mark_running(self, request_id: str, *, now: str) -> None:
        self._update_one(
            """UPDATE trial_requests
               SET execution_status='running', started_at=COALESCE(started_at, ?), last_alive_at=?
               WHERE request_id=? AND execution_status='queued'""",
            (now, now, request_id),
        )

    def touch_alive(self, request_id: str, *, now: str) -> bool:
        cursor = self.connection.execute(
            """UPDATE trial_requests SET last_alive_at=?
               WHERE request_id=? AND execution_status='running'""",
            (now, request_id),
        )
        self.connection.commit()
        return cursor.rowcount == 1

    def authorize_invocation(
        self, request_id: str, core_run_id: str, *, now: str
    ) -> bool:
        cursor = self.connection.execute(
            """UPDATE trial_requests
               SET provider_invocation_count=provider_invocation_count+1, last_alive_at=?
               WHERE request_id=? AND core_run_id=? AND execution_status='running'""",
            (now, request_id, core_run_id),
        )
        self.connection.commit()
        return cursor.rowcount == 1

    def mark_terminal(
        self,
        request_id: str,
        *,
        status: str,
        now: str,
        processing_duration_ms: int | None = None,
        reject_reason: str | None = None,
    ) -> None:
        if status not in {"needs_review", "succeeded", "failed", "cancelled", "interrupted"}:
            raise ContractError("invalid Trial terminal status")
        terminal_at = None if status == "needs_review" else now
        interrupted_at = now if status == "interrupted" else None
        cursor = self.connection.execute(
            """UPDATE trial_requests
               SET execution_status=CASE
                       WHEN execution_status='interrupted' THEN 'interrupted'
                       ELSE ?
                   END,
                   terminal_at=COALESCE(terminal_at, ?),
                   interrupted_at=COALESCE(?, interrupted_at),
                   processing_duration_ms=COALESCE(?, processing_duration_ms),
                   reject_reason=COALESCE(?, reject_reason)
               WHERE request_id=? AND execution_status IN ('queued', 'running', 'interrupted')""",
            (
                status,
                terminal_at,
                interrupted_at,
                processing_duration_ms,
                reject_reason,
                request_id,
            ),
        )
        self.connection.commit()
        current_status = str(self.request(request_id)["execution_status"])
        if cursor.rowcount != 1 and current_status != status:
            raise ContractError("Trial request cannot enter the requested terminal state")

    def sweep_stale(self, *, stale_before: str, now: str) -> list[str]:
        with self.transaction() as tx:
            rows = tx.execute(
                """SELECT request_id FROM trial_requests
                   WHERE execution_status='running' AND last_alive_at<?""",
                (stale_before,),
            ).fetchall()
            request_ids = [str(row[0]) for row in rows]
            for request_id in request_ids:
                tx.execute(
                    """UPDATE trial_requests
                       SET execution_status='interrupted', interrupted_at=?, terminal_at=?,
                           reject_reason='stale_worker'
                       WHERE request_id=? AND execution_status='running' AND last_alive_at<?""",
                    (now, now, request_id, stale_before),
                )
        return request_ids

    def classify_unknown(self, *, created_before: str, now: str) -> list[str]:
        with self.transaction() as tx:
            rows = tx.execute(
                """SELECT request_id FROM trial_requests
                   WHERE budget_released_at IS NULL AND created_at<?
                     AND execution_status IN (
                         'needs_review', 'succeeded', 'failed', 'cancelled', 'interrupted'
                     )""",
                (created_before,),
            ).fetchall()
            request_ids = [str(row[0]) for row in rows]
            for request_id in request_ids:
                tx.execute(
                    """UPDATE trial_requests
                       SET cost_status='unknown', calculated_cost_micros=NULL,
                           estimated_unknown_cost_micros=estimated_max_cost_micros,
                           budget_released_at=?
                       WHERE request_id=? AND budget_released_at IS NULL""",
                    (now, request_id),
                )
        return request_ids

    def upsert_usage(self, value: Mapping[str, Any]) -> None:
        columns = (
            "invocation_id", "request_id", "operation", "provider", "resolved_model",
            "invocation_status", "input_tokens", "output_tokens", "cached_tokens",
            "reasoning_tokens", "total_tokens", "usage_json", "usage_source",
            "pricing_version", "calculated_cost_micros", "currency", "cost_status",
            "created_at", "updated_at",
        )
        values = tuple(value.get(column) for column in columns)
        self.connection.execute(
            f"""INSERT INTO trial_usage({','.join(columns)})
                VALUES ({','.join('?' for _ in columns)})
                ON CONFLICT(invocation_id) DO UPDATE SET
                    invocation_status=excluded.invocation_status,
                    resolved_model=excluded.resolved_model,
                    input_tokens=excluded.input_tokens,
                    output_tokens=excluded.output_tokens,
                    cached_tokens=excluded.cached_tokens,
                    reasoning_tokens=excluded.reasoning_tokens,
                    total_tokens=excluded.total_tokens,
                    usage_json=excluded.usage_json,
                    usage_source=excluded.usage_source,
                    pricing_version=excluded.pricing_version,
                    calculated_cost_micros=excluded.calculated_cost_micros,
                    currency=excluded.currency,
                    cost_status=excluded.cost_status,
                    updated_at=excluded.updated_at""",
            values,
        )
        self.connection.commit()

    def finalize_cost(self, request_id: str, *, now: str) -> str:
        request = self.request(request_id)
        rows = self.connection.execute(
            "SELECT cost_status, calculated_cost_micros FROM trial_usage WHERE request_id=?",
            (request_id,),
        ).fetchall()
        if len(rows) < int(request["provider_invocation_count"]) or any(
            row["cost_status"] in {"pending", "unknown"} for row in rows
        ):
            return "pending"
        total = sum(int(row["calculated_cost_micros"] or 0) for row in rows)
        status = "calculated" if total or any(
            row["cost_status"] == "calculated" for row in rows
        ) else "not_incurred"
        self.connection.execute(
            """UPDATE trial_requests
               SET cost_status=?, calculated_cost_micros=?, budget_released_at=?
               WHERE request_id=? AND budget_released_at IS NULL""",
            (status, total, now, request_id),
        )
        self.connection.commit()
        return status

    def record_result(self, request_id: str, value: Mapping[str, Any], *, now: str) -> None:
        encoded = json.dumps(dict(value), sort_keys=True, separators=(",", ":"))
        self._update_one(
            """UPDATE trial_requests SET result_object_json=?, result_published_at=?
               WHERE request_id=?""",
            (encoded, now, request_id),
        )

    def request(self, request_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM trial_requests WHERE request_id=?", (request_id,)
        ).fetchone()
        if row is None:
            raise TrialNotFoundError("unknown Trial request")
        return cast(sqlite3.Row, row)

    def latest_job(self, job_id: str, visitor_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            """SELECT * FROM trial_requests WHERE job_id=? AND visitor_id=?
               ORDER BY created_at DESC, request_id DESC LIMIT 1""",
            (job_id, visitor_id),
        ).fetchone()
        if row is None:
            raise TrialNotFoundError("unknown Trial job")
        return cast(sqlite3.Row, row)

    def jobs(self, visitor_id: str) -> list[sqlite3.Row]:
        return self.connection.execute(
            """SELECT r.* FROM trial_requests r
               JOIN (SELECT job_id, MAX(created_at || request_id) AS latest
                     FROM trial_requests WHERE visitor_id=? GROUP BY job_id) x
                 ON x.job_id=r.job_id AND x.latest=(r.created_at || r.request_id)
               WHERE r.visitor_id=? ORDER BY r.created_at DESC, r.request_id DESC""",
            (visitor_id, visitor_id),
        ).fetchall()

    def requests(self, *, limit: int = 100) -> list[sqlite3.Row]:
        return self.connection.execute(
            "SELECT * FROM trial_requests ORDER BY created_at DESC, request_id DESC LIMIT ?",
            (limit,),
        ).fetchall()

    def expired_workspace_jobs(self, *, terminal_before: str) -> list[str]:
        rows = self.connection.execute(
            """SELECT job_id, MAX(terminal_at) AS last_terminal
               FROM trial_requests
               WHERE execution_status IN ('failed', 'cancelled', 'interrupted')
                 AND terminal_at IS NOT NULL
               GROUP BY job_id HAVING last_terminal<?""",
            (terminal_before,),
        ).fetchall()
        return [str(row["job_id"]) for row in rows]

    def active_concurrency(self) -> int:
        return int(self.connection.execute(
            """SELECT COUNT(*) FROM trial_requests
               WHERE execution_status IN ('queued', 'running')"""
        ).fetchone()[0])

    def summary(
        self, *, accounting_date: str, online_after: str, default_daily_budget_micros: int
    ) -> dict[str, Any]:
        day_start, day_end = accounting_day_bounds(accounting_date)
        online = int(self.connection.execute(
            "SELECT COUNT(*) FROM trial_visitors WHERE last_seen_at>=?", (online_after,)
        ).fetchone()[0])
        total_uv = int(self.connection.execute(
            "SELECT COUNT(*) FROM trial_visitors"
        ).fetchone()[0])
        today_uv = int(self.connection.execute(
            """SELECT COUNT(*) FROM trial_visitors
               WHERE last_seen_at>=? AND last_seen_at<?""",
            (day_start, day_end),
        ).fetchone()[0])
        new_visitors = int(self.connection.execute(
            """SELECT COUNT(*) FROM trial_visitors
               WHERE first_seen_at>=? AND first_seen_at<?""",
            (day_start, day_end),
        ).fetchone()[0])
        returning_visitors = int(self.connection.execute(
            """SELECT COUNT(*) FROM trial_visitors
               WHERE first_seen_at<? AND last_seen_at>=? AND last_seen_at<?""",
            (day_start, day_start, day_end),
        ).fetchone()[0])
        activated_visitors = int(self.connection.execute(
            "SELECT COUNT(*) FROM trial_visitors WHERE first_job_at IS NOT NULL"
        ).fetchone()[0])
        repeat_visitors = int(self.connection.execute(
            "SELECT COUNT(*) FROM trial_visitors WHERE second_active_day_at IS NOT NULL"
        ).fetchone()[0])
        request_rows = self.connection.execute(
            "SELECT * FROM trial_requests WHERE accounting_date=?", (accounting_date,)
        ).fetchall()
        accepted = [row for row in request_rows if row["execution_status"] != "rejected"]
        quality = [row for row in accepted if row["execution_status"] in {"succeeded", "failed"}]
        successes = sum(row["execution_status"] == "succeeded" for row in quality)
        failures = sum(row["execution_status"] == "failed" for row in quality)
        processing = sorted(
            int(row["processing_duration_ms"])
            for row in accepted
            if row["processing_duration_ms"] is not None
        )
        calculated_costs = sorted(
            int(row["calculated_cost_micros"])
            for row in accepted
            if row["cost_status"] in {"calculated", "not_incurred"}
            and row["calculated_cost_micros"] is not None
        )
        calculated_total = sum(calculated_costs)
        audio_total = sum(int(row["audio_duration_ms"] or 0) for row in accepted)
        active_budget = sum(
            int(row["estimated_max_cost_micros"])
            for row in accepted
            if row["budget_released_at"] is None
        )
        unknown = sum(int(row["estimated_unknown_cost_micros"] or 0) for row in accepted)
        stats = self.connection.execute(
            "SELECT * FROM trial_stats WHERE stats_key='global'"
        ).fetchone()
        control = self.current_control(default_daily_budget_micros)
        return {
            "online_now": online,
            "today_cookie_uv": today_uv,
            "new_visitors_today": new_visitors,
            "returning_visitors_today": returning_visitors,
            "total_cookie_uv": total_uv,
            "second_active_day_rate": (
                repeat_visitors / activated_visitors if activated_visitors else None
            ),
            "peak_online": int(stats["peak_online"]) if stats else 0,
            "peak_online_at": stats["peak_online_at"] if stats else None,
            "today_jobs": len({str(row["job_id"]) for row in accepted}),
            "today_user_runs": len(accepted),
            "today_audio_minutes": round(audio_total / 60_000, 2),
            "successes": successes,
            "failures": failures,
            "interrupted": sum(row["execution_status"] == "interrupted" for row in accepted),
            "success_rate": successes / len(quality) if quality else None,
            "failure_rate": failures / len(quality) if quality else None,
            "quality_denominator": len(quality),
            "processing_ms_average": _average(processing),
            "processing_ms_p50": _percentile(processing, 0.50),
            "processing_ms_p95": _percentile(processing, 0.95),
            "today_calculated_cost_micros": calculated_total,
            "active_budget_occupancy_micros": active_budget,
            "estimated_unknown_cost_micros": unknown,
            "cost_coverage": len(calculated_costs) / len(accepted) if accepted else None,
            "unknown_request_count": sum(row["cost_status"] == "unknown" for row in accepted),
            "average_cost_per_job_micros": _average(calculated_costs),
            "average_cost_per_audio_minute_micros": (
                round(calculated_total / (audio_total / 60_000)) if audio_total else None
            ),
            "cost_per_job_p50_micros": _percentile(calculated_costs, 0.50),
            "cost_per_job_p95_micros": _percentile(calculated_costs, 0.95),
            "reject_reasons": _counts(
                str(row["reject_reason"]) for row in request_rows if row["reject_reason"]
            ),
            **control,
        }

    def anonymize_before(self, *, observed_before: str) -> dict[str, int]:
        with self.transaction() as tx:
            requests = tx.execute(
                """UPDATE trial_requests SET ip_hmac=NULL, fingerprint_hmac=NULL
                   WHERE created_at<? AND (ip_hmac IS NOT NULL OR fingerprint_hmac IS NOT NULL)""",
                (observed_before,),
            ).rowcount
            visitors = tx.execute(
                """UPDATE trial_visitors SET fingerprint_hmac=NULL,
                       fingerprint_observed_at=NULL
                   WHERE fingerprint_observed_at<? AND fingerprint_hmac IS NOT NULL""",
                (observed_before,),
            ).rowcount
        return {"requests": requests, "visitors": visitors}

    def _update_one(self, sql: str, parameters: tuple[object, ...]) -> None:
        cursor = self.connection.execute(sql, parameters)
        self.connection.commit()
        if cursor.rowcount != 1:
            raise TrialNotFoundError("unknown Trial request or invalid state")


def now_utc() -> str:
    return _iso(datetime.now(timezone.utc))


def accounting_date(now: str) -> str:
    """Trial reporting day in the deployment's frozen China Standard Time (UTC+8)."""
    return _parse(now).astimezone(timezone(timedelta(hours=8))).date().isoformat()


def accounting_day_bounds(value: str) -> tuple[str, str]:
    """UTC bounds for one frozen China Standard Time reporting day."""
    local_timezone = timezone(timedelta(hours=8))
    local_start = datetime.fromisoformat(value).replace(tzinfo=local_timezone)
    return _iso(local_start), _iso(local_start + timedelta(days=1))


def before(now: str, seconds: int) -> str:
    return _iso(_parse(now) - timedelta(seconds=seconds))


def public_request(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "request_id": row["request_id"],
        "job_id": row["job_id"],
        "action_kind": row["action_kind"],
        "status": row["execution_status"],
        "reject_reason": row["reject_reason"],
        "created_at": row["created_at"],
        "started_at": row["started_at"],
        "terminal_at": row["terminal_at"],
        "audio_duration_ms": row["audio_duration_ms"],
        "processing_duration_ms": row["processing_duration_ms"],
        "cost_status": row["cost_status"],
        "result_available": row["result_object_json"] is not None,
    }


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _admission_message(reason: str) -> str:
    return {
        "visitor_blocked": "This trial visitor is blocked.",
        "trial_paused": "The anonymous trial is temporarily paused.",
        "visitor_hourly_quota": "Your hourly trial limit has been reached.",
        "visitor_daily_quota": "Your daily trial limit has been reached.",
        "visitor_daily_audio_quota": "Your daily audio-minute limit has been reached.",
        "ip_hourly_quota": "This network has reached its hourly trial limit.",
        "ip_daily_quota": "This network has reached its daily trial limit.",
        "visitor_concurrency": "You already have the maximum number of active trial jobs.",
        "ip_concurrency": "This network has reached its active-job limit.",
        "global_concurrency": "The trial service is currently at capacity.",
        "daily_budget": "Today's trial budget has been used.",
        "disk_capacity": "The trial workspace does not have enough safe free space.",
        "storage_not_ready": "Trial object storage is not ready.",
        "invalid_media": "The uploaded media does not meet the trial limits.",
    }.get(reason, "The trial request was rejected.")


def _counts(values: Iterator[str] | Any) -> dict[str, int]:
    result: dict[str, int] = {}
    for value in values:
        result[value] = result.get(value, 0) + 1
    return result


def _average(values: list[int]) -> int | None:
    return round(sum(values) / len(values)) if values else None


def _percentile(values: list[int], fraction: float) -> int | None:
    if not values:
        return None
    if fraction == 0.5:
        return round(median(values))
    index = max(0, math.ceil(len(values) * fraction) - 1)
    return values[index]
