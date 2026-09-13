from __future__ import annotations

import sqlite3
from pathlib import Path

from cueflow.errors import TrialStoreError
from cueflow.schema import utc_now

TRIAL_SCHEMA_VERSION = 1

TRIAL_SCHEMA_SQL = """
CREATE TABLE trial_schema (
    singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
    schema_version INTEGER NOT NULL,
    applied_at TEXT NOT NULL
);

CREATE TABLE trial_visitors (
    visitor_id TEXT PRIMARY KEY,
    fingerprint_hmac TEXT,
    fingerprint_observed_at TEXT,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    first_job_at TEXT,
    second_active_day_at TEXT,
    active_day_count INTEGER NOT NULL DEFAULT 0 CHECK(active_day_count >= 0),
    last_active_date TEXT,
    status TEXT NOT NULL DEFAULT 'normal' CHECK(status IN ('normal', 'blocked'))
);

CREATE TABLE trial_requests (
    request_id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL,
    visitor_id TEXT NOT NULL REFERENCES trial_visitors(visitor_id),
    action_kind TEXT NOT NULL CHECK(action_kind IN ('create', 'retry', 'resume')),
    accounting_date TEXT NOT NULL,
    ip_hmac TEXT,
    fingerprint_hmac TEXT,
    core_run_id TEXT,
    execution_status TEXT NOT NULL CHECK(execution_status IN (
        'rejected', 'queued', 'running', 'needs_review', 'succeeded', 'failed',
        'cancelled', 'interrupted'
    )),
    reject_reason TEXT,
    created_at TEXT NOT NULL,
    started_at TEXT,
    terminal_at TEXT,
    last_alive_at TEXT,
    interrupted_at TEXT,
    audio_duration_ms INTEGER CHECK(audio_duration_ms IS NULL OR audio_duration_ms >= 0),
    processing_duration_ms INTEGER CHECK(
        processing_duration_ms IS NULL OR processing_duration_ms >= 0
    ),
    provider_invocation_count INTEGER NOT NULL DEFAULT 0 CHECK(provider_invocation_count >= 0),
    estimated_max_cost_micros INTEGER NOT NULL CHECK(estimated_max_cost_micros >= 0),
    calculated_cost_micros INTEGER CHECK(
        calculated_cost_micros IS NULL OR calculated_cost_micros >= 0
    ),
    estimated_unknown_cost_micros INTEGER CHECK(
        estimated_unknown_cost_micros IS NULL OR estimated_unknown_cost_micros >= 0
    ),
    cost_status TEXT NOT NULL CHECK(cost_status IN (
        'pending', 'calculated', 'not_incurred', 'unknown'
    )),
    budget_released_at TEXT,
    source_object_json TEXT,
    result_object_json TEXT,
    result_published_at TEXT
);

CREATE INDEX trial_requests_visitor_created
    ON trial_requests(visitor_id, created_at, request_id);
CREATE INDEX trial_requests_ip_created
    ON trial_requests(ip_hmac, created_at, request_id);
CREATE INDEX trial_requests_job_created
    ON trial_requests(job_id, created_at, request_id);
CREATE INDEX trial_requests_execution_alive
    ON trial_requests(execution_status, last_alive_at);
CREATE INDEX trial_requests_accounting_date
    ON trial_requests(accounting_date, created_at);

CREATE TABLE trial_usage (
    invocation_id TEXT PRIMARY KEY,
    request_id TEXT NOT NULL REFERENCES trial_requests(request_id),
    operation TEXT NOT NULL,
    provider TEXT NOT NULL,
    resolved_model TEXT,
    invocation_status TEXT NOT NULL,
    input_tokens INTEGER,
    output_tokens INTEGER,
    cached_tokens INTEGER,
    reasoning_tokens INTEGER,
    total_tokens INTEGER,
    usage_json TEXT,
    usage_source TEXT NOT NULL,
    pricing_version TEXT,
    calculated_cost_micros INTEGER CHECK(
        calculated_cost_micros IS NULL OR calculated_cost_micros >= 0
    ),
    currency TEXT NOT NULL,
    cost_status TEXT NOT NULL CHECK(cost_status IN (
        'pending', 'calculated', 'not_incurred', 'unknown'
    )),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX trial_usage_request ON trial_usage(request_id, created_at, invocation_id);

CREATE TABLE trial_control (
    control_revision INTEGER PRIMARY KEY AUTOINCREMENT,
    action TEXT NOT NULL CHECK(action IN (
        'pause', 'resume', 'set_daily_budget_override', 'clear_daily_budget_override'
    )),
    old_value_json TEXT NOT NULL,
    new_value_json TEXT NOT NULL,
    reason TEXT NOT NULL CHECK(length(trim(reason)) > 0),
    created_at TEXT NOT NULL
);

CREATE TABLE trial_stats (
    stats_key TEXT PRIMARY KEY CHECK(stats_key = 'global'),
    peak_online INTEGER NOT NULL DEFAULT 0 CHECK(peak_online >= 0),
    peak_online_at TEXT,
    updated_at TEXT NOT NULL
);
"""

TRIAL_TABLE_COLUMNS: dict[str, tuple[str, ...]] = {
    "trial_schema": ("singleton", "schema_version", "applied_at"),
    "trial_visitors": (
        "visitor_id", "fingerprint_hmac", "fingerprint_observed_at", "first_seen_at",
        "last_seen_at", "first_job_at", "second_active_day_at", "active_day_count",
        "last_active_date", "status",
    ),
    "trial_requests": (
        "request_id", "job_id", "visitor_id", "action_kind", "accounting_date",
        "ip_hmac", "fingerprint_hmac", "core_run_id", "execution_status",
        "reject_reason", "created_at", "started_at", "terminal_at", "last_alive_at",
        "interrupted_at", "audio_duration_ms", "processing_duration_ms",
        "provider_invocation_count",
        "estimated_max_cost_micros", "calculated_cost_micros",
        "estimated_unknown_cost_micros", "cost_status", "budget_released_at",
        "source_object_json", "result_object_json", "result_published_at",
    ),
    "trial_usage": (
        "invocation_id", "request_id", "operation", "provider", "resolved_model",
        "invocation_status", "input_tokens", "output_tokens", "cached_tokens",
        "reasoning_tokens", "total_tokens", "usage_json", "usage_source",
        "pricing_version", "calculated_cost_micros", "currency", "cost_status",
        "created_at", "updated_at",
    ),
    "trial_control": (
        "control_revision", "action", "old_value_json", "new_value_json", "reason",
        "created_at",
    ),
    "trial_stats": ("stats_key", "peak_online", "peak_online_at", "updated_at"),
}


def initialize_trial_database(path: Path) -> None:
    if not path.is_absolute():
        raise TrialStoreError("Trial database path must be absolute")
    if path.exists():
        raise TrialStoreError("Trial database already exists")
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    try:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.executescript(TRIAL_SCHEMA_SQL)
        now = utc_now()
        connection.execute(
            "INSERT INTO trial_schema(singleton, schema_version, applied_at) VALUES (1, ?, ?)",
            (TRIAL_SCHEMA_VERSION, now),
        )
        connection.execute(
            "INSERT INTO trial_stats(stats_key, peak_online, updated_at) VALUES ('global', 0, ?)",
            (now,),
        )
        connection.commit()
    except BaseException:
        connection.close()
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    connection.close()


def validate_trial_schema(connection: sqlite3.Connection) -> None:
    row = connection.execute(
        "SELECT schema_version FROM trial_schema WHERE singleton=1"
    ).fetchone()
    if row is None or int(row[0]) != TRIAL_SCHEMA_VERSION:
        raise TrialStoreError("Trial database requires schema version 1")
    for table, expected in TRIAL_TABLE_COLUMNS.items():
        actual = tuple(
            str(item[1]) for item in connection.execute(f"PRAGMA table_info({table})")
        )
        if actual != expected:
            raise TrialStoreError(
                f"Trial table columns do not match for {table}: "
                f"expected {list(expected)}, found {list(actual)}"
            )
