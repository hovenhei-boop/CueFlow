from __future__ import annotations

from pathlib import Path

from cueflow.config import TrialConfig
from cueflow.trial_migrations import initialize_trial_database
from cueflow.trial_store import TrialAdmission, TrialStore


def trial_config(tmp_path: Path, **changes: object) -> TrialConfig:
    values: dict[str, object] = {
        "database_path": (tmp_path / "trial.sqlite3").resolve(),
        "work_root": (tmp_path / "work").resolve(),
        "public_origin": "https://trial.example",
        "ip_hmac_secret": "i" * 32,
        "fingerprint_hmac_secret": "f" * 32,
        "operator_secret": "o" * 32,
        "pricing_path": (Path(__file__).parent / "fixtures" / "trial_pricing.json").resolve(),
        "disk_safety_margin_bytes": 0,
        "workspace_expansion_factor": 1.0,
        "global_concurrency": 2,
    }
    values.update(changes)
    return TrialConfig(**values)  # type: ignore[arg-type]


def initialized_store(config: TrialConfig) -> TrialStore:
    initialize_trial_database(config.database_path)
    return TrialStore(config.database_path)


def admission(
    *,
    request_id: str = "req_" + "1" * 32,
    job_id: str = "job_" + "1" * 32,
    visitor_id: str = "v_" + "1" * 32,
    created_at: str = "2026-09-13T01:00:00.000000Z",
    audio_duration_ms: int = 60_000,
    estimated_max_cost_micros: int = 1_000_000,
    action_kind: str = "create",
) -> TrialAdmission:
    return TrialAdmission(
        request_id=request_id,
        job_id=job_id,
        visitor_id=visitor_id,
        action_kind=action_kind,
        ip_hmac="hmac-sha256:" + "a" * 64,
        fingerprint_hmac="hmac-sha256:" + "b" * 64,
        audio_duration_ms=audio_duration_ms,
        estimated_max_cost_micros=estimated_max_cost_micros,
        created_at=created_at,
        accounting_date="2026-09-13",
    )
