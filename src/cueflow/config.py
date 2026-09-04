from __future__ import annotations

import os
import shutil
from dataclasses import asdict, dataclass
from typing import Any

SCHEMA_VERSION = "7.0.0"
COMPONENT_VERSION = "0.5.2"
ATOMIZER_VERSION = "0.2.0"

QWEN_ASR_MODEL = "qwen-audio-3.0-asr-flash-filetrans"
DOUBAO_ASR_MODEL = "bigmodel"
GLM_ASR_MODEL = "glm-asr-2512"
QWEN_CORRECTION_MODEL = "qwen3.8-max-2026-09-02"
KIMI_CORRECTION_MODEL = "kimi-k3"
ATA_PROVIDER = "volcengine-ata"

MAX_USER_KEYWORDS = 100
MAX_SOURCE_DURATION_MS = 5 * 60 * 60 * 1000
MAX_SOURCE_BYTES = 512_000_000
QWEN_HOTWORD_WEIGHT = 5


@dataclass(frozen=True)
class MediaPrepConfig:
    version: str = "0.1.0"
    sample_rate_hz: int = 16_000
    channels: int = 1
    sample_format: str = "s16le"
    opening_scan_limit_ms: int = 50_000


@dataclass(frozen=True)
class EvidenceWindowConfig:
    version: str = "0.1.0"
    padding_ms: int = 3_000
    merge_gap_ms: int = 2_000
    max_duration_ms: int = 30_000
    max_bytes: int = 25_000_000


@dataclass(frozen=True)
class SegmenterConfig:
    version: str = "0.2.0"
    max_display_units: int = 10
    removable_punctuation: str = "。；;？?！!—–."
    comma_punctuation: str = "，,"
    english_clause_starters: tuple[str, ...] = (
        "when",
        "while",
        "because",
        "although",
        "if",
        "unless",
        "who",
        "which",
        "that",
    )


@dataclass(frozen=True)
class QaRulesetConfig:
    version: str = "0.2.0"


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
        "evidence_windows": asdict(EvidenceWindowConfig()),
        "segmenter": asdict(SegmenterConfig()),
        "qa": asdict(QaRulesetConfig()),
        "qwen_asr_model": QWEN_ASR_MODEL,
        "doubao_asr_model": DOUBAO_ASR_MODEL,
        "glm_asr_model": GLM_ASR_MODEL,
        "qwen_correction_model": QWEN_CORRECTION_MODEL,
        "kimi_correction_model": KIMI_CORRECTION_MODEL,
        "alignment_provider": ATA_PROVIDER,
        "max_user_keywords": MAX_USER_KEYWORDS,
        "max_source_duration_ms_exclusive": MAX_SOURCE_DURATION_MS,
        "max_source_bytes_exclusive": MAX_SOURCE_BYTES,
        "runtime": {"ffmpeg": bool(chosen.ffmpeg), "ffprobe": bool(chosen.ffprobe)},
    }
