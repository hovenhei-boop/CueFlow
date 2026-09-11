from __future__ import annotations

import os
import shutil
from dataclasses import asdict, dataclass
from typing import Any

SCHEMA_VERSION = "12.0.0"
COMPONENT_VERSION = "0.5.4"

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
