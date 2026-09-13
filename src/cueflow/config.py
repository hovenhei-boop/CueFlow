from __future__ import annotations

import os
import shutil
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

APP_VERSION = "0.6.3"
SCHEMA_VERSION = "12.0.0"

QWEN_ASR_MODEL = "qwen-audio-3.0-asr-flash-filetrans"
DOUBAO_ASR_MODEL = "bigmodel"
GLM_SELECTION_MODEL = "glm-5.2"
QWEN_CORRECTION_MODEL = "qwen3.8-max-2026-09-02"
KIMI_CORRECTION_MODEL = "kimi-k3"
ATA_PROVIDER = "volcengine-ata"

MAX_USER_KEYWORDS = 100
MAX_SOURCE_DURATION_MS = 5 * 60 * 60 * 1000
MAX_SOURCE_BYTES = 512_000_000
QWEN_HOTWORD_WEIGHT = 5

TRIAL_MAX_SOURCE_BYTES = 500_000_000
TRIAL_MAX_SOURCE_DURATION_MS = 60 * 60 * 1000


@dataclass(frozen=True)
class MediaPrepConfig:
    version: str = "0.1.0"
    sample_rate_hz: int = 16_000
    channels: int = 1
    sample_format: str = "s16le"
    opening_scan_limit_ms: int = 50_000


@dataclass(frozen=True)
class SelectionConfig:
    version: str = "0.1.0"
    context_chars: int = 400
    max_context_chars: int = 500
    max_cases: int = 8
    max_input_bytes: int = 48_000
    max_output_tokens: int = 1_024
    web_search: bool = True


@dataclass(frozen=True)
class CloudJobConfig:
    poll_interval_seconds: float = 2.0
    poll_timeout_seconds: float = 900.0
    request_timeout_seconds: float = 60.0


@dataclass(frozen=True)
class TosConfig:
    url_ttl_seconds: int = 7 * 24 * 60 * 60
    object_prefix: str = "cueflow/media"


@dataclass(frozen=True)
class TrialConfig:
    """Single-host anonymous Trial settings, loaded only by the Trial entrypoint."""

    database_path: Path
    work_root: Path
    public_origin: str
    ip_hmac_secret: str
    fingerprint_hmac_secret: str
    operator_secret: str
    pricing_path: Path
    max_source_bytes: int = TRIAL_MAX_SOURCE_BYTES
    max_source_duration_ms: int = TRIAL_MAX_SOURCE_DURATION_MS
    daily_budget_micros: int = 100_000_000
    visitor_hourly_jobs: int = 5
    visitor_daily_jobs: int = 20
    visitor_daily_audio_ms: int = 240 * 60 * 1000
    ip_hourly_jobs: int = 15
    ip_daily_jobs: int = 50
    visitor_concurrency: int = 2
    ip_concurrency: int = 4
    global_concurrency: int = 20
    stale_request_seconds: int = 4 * 60 * 60
    stale_sweep_seconds: int = 5 * 60
    unknown_budget_hold_seconds: int = 24 * 60 * 60
    alive_interval_seconds: int = 45
    online_window_seconds: int = 5 * 60
    workspace_retention_seconds: int = 24 * 60 * 60
    disk_max_used_ratio: float = 0.85
    workspace_expansion_factor: float = 3.0
    disk_safety_margin_bytes: int = 2_000_000_000
    source_prefix: str = "trial/source"
    work_prefix: str = "trial/work"
    result_prefix: str = "trial/result"
    result_url_ttl_seconds: int = 10 * 60

    def __post_init__(self) -> None:
        integer_limits = (
            self.max_source_bytes,
            self.max_source_duration_ms,
            self.daily_budget_micros,
            self.visitor_hourly_jobs,
            self.visitor_daily_jobs,
            self.visitor_daily_audio_ms,
            self.ip_hourly_jobs,
            self.ip_daily_jobs,
            self.visitor_concurrency,
            self.ip_concurrency,
            self.global_concurrency,
            self.stale_request_seconds,
            self.stale_sweep_seconds,
            self.unknown_budget_hold_seconds,
            self.alive_interval_seconds,
            self.online_window_seconds,
            self.workspace_retention_seconds,
            self.result_url_ttl_seconds,
        )
        if any(value <= 0 for value in integer_limits):
            raise ValueError("Trial limits and intervals must be positive")
        if not 0 < self.disk_max_used_ratio < 1:
            raise ValueError("TRIAL_DISK_MAX_USED_RATIO must be between zero and one")
        if self.workspace_expansion_factor < 1 or self.disk_safety_margin_bytes < 0:
            raise ValueError("Trial disk expansion and safety margin are invalid")
        if not all(path.is_absolute() for path in (
            self.database_path, self.work_root, self.pricing_path
        )):
            raise ValueError("Trial database, work root, and pricing paths must be absolute")

    @classmethod
    def from_environment(cls) -> TrialConfig:
        def required(name: str) -> str:
            value = os.getenv(name, "").strip()
            if not value:
                raise ValueError(f"{name} is required")
            return value

        daily_budget = os.getenv("TRIAL_DAILY_BUDGET_MICROS", "100000000")
        return cls(
            database_path=Path(required("TRIAL_DATABASE_PATH")).resolve(),
            work_root=Path(required("TRIAL_WORK_ROOT")).resolve(),
            public_origin=required("TRIAL_PUBLIC_ORIGIN"),
            ip_hmac_secret=required("TRIAL_IP_HMAC_SECRET"),
            fingerprint_hmac_secret=required("TRIAL_FINGERPRINT_HMAC_SECRET"),
            operator_secret=required("TRIAL_OPERATOR_SECRET"),
            pricing_path=Path(required("TRIAL_PRICING_PATH")).resolve(),
            daily_budget_micros=int(daily_budget),
            max_source_bytes=int(
                os.getenv("TRIAL_MAX_SOURCE_BYTES", str(TRIAL_MAX_SOURCE_BYTES))
            ),
            max_source_duration_ms=int(
                os.getenv(
                    "TRIAL_MAX_SOURCE_DURATION_MS", str(TRIAL_MAX_SOURCE_DURATION_MS)
                )
            ),
            visitor_hourly_jobs=int(os.getenv("TRIAL_VISITOR_HOURLY_JOBS", "5")),
            visitor_daily_jobs=int(os.getenv("TRIAL_VISITOR_DAILY_JOBS", "20")),
            visitor_daily_audio_ms=int(
                os.getenv("TRIAL_VISITOR_DAILY_AUDIO_MS", str(240 * 60 * 1000))
            ),
            ip_hourly_jobs=int(os.getenv("TRIAL_IP_HOURLY_JOBS", "15")),
            ip_daily_jobs=int(os.getenv("TRIAL_IP_DAILY_JOBS", "50")),
            visitor_concurrency=int(os.getenv("TRIAL_VISITOR_CONCURRENCY", "2")),
            ip_concurrency=int(os.getenv("TRIAL_IP_CONCURRENCY", "4")),
            global_concurrency=int(os.getenv("TRIAL_GLOBAL_CONCURRENCY", "20")),
            stale_request_seconds=int(os.getenv("TRIAL_STALE_REQUEST_SECONDS", "14400")),
            stale_sweep_seconds=int(os.getenv("TRIAL_STALE_SWEEP_SECONDS", "300")),
            unknown_budget_hold_seconds=int(
                os.getenv("TRIAL_UNKNOWN_BUDGET_HOLD_SECONDS", "86400")
            ),
            alive_interval_seconds=int(os.getenv("TRIAL_ALIVE_INTERVAL_SECONDS", "45")),
            online_window_seconds=int(os.getenv("TRIAL_ONLINE_WINDOW_SECONDS", "300")),
            workspace_retention_seconds=int(
                os.getenv("TRIAL_WORKSPACE_RETENTION_SECONDS", "86400")
            ),
            disk_max_used_ratio=float(os.getenv("TRIAL_DISK_MAX_USED_RATIO", "0.85")),
            workspace_expansion_factor=float(
                os.getenv("TRIAL_WORKSPACE_EXPANSION_FACTOR", "3")
            ),
            disk_safety_margin_bytes=int(
                os.getenv("TRIAL_DISK_SAFETY_MARGIN_BYTES", "2000000000")
            ),
            source_prefix=os.getenv("TRIAL_SOURCE_PREFIX", "trial/source"),
            work_prefix=os.getenv("TRIAL_WORK_PREFIX", "trial/work"),
            result_prefix=os.getenv("TRIAL_RESULT_PREFIX", "trial/result"),
            result_url_ttl_seconds=int(
                os.getenv("TRIAL_RESULT_URL_TTL_SECONDS", "600")
            ),
        )


@dataclass(frozen=True)
class RuntimeConfig:
    ffmpeg: str
    ffprobe: str

    @classmethod
    def detect(cls) -> RuntimeConfig:
        return cls(
            ffmpeg=os.getenv("CUEFLOW_FFMPEG") or shutil.which("ffmpeg") or "",
            ffprobe=os.getenv("CUEFLOW_FFPROBE") or shutil.which("ffprobe") or "",
        )


def result_config(runtime: RuntimeConfig | None = None) -> dict[str, Any]:
    chosen = runtime or RuntimeConfig.detect()
    return {
        "media": asdict(MediaPrepConfig()),
        "selection": asdict(SelectionConfig()),
        "qwen_asr_model": QWEN_ASR_MODEL,
        "doubao_asr_model": DOUBAO_ASR_MODEL,
        "glm_selection_model": GLM_SELECTION_MODEL,
        "qwen_correction_model": QWEN_CORRECTION_MODEL,
        "kimi_correction_model": KIMI_CORRECTION_MODEL,
        "ata_provider": ATA_PROVIDER,
        "ata_punctuation_mode": "3",
        "srt_serializer": "utterances-v1",
        "max_user_keywords": MAX_USER_KEYWORDS,
        "max_source_duration_ms_exclusive": MAX_SOURCE_DURATION_MS,
        "max_source_bytes_exclusive": MAX_SOURCE_BYTES,
        "runtime": {"ffmpeg": bool(chosen.ffmpeg), "ffprobe": bool(chosen.ffprobe)},
    }
