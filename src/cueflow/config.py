from __future__ import annotations

import os
import shutil
from dataclasses import asdict, dataclass
from importlib import import_module
from typing import Any

SCHEMA_VERSION = "4.0.0"
COMPONENT_VERSION = "0.5.0"
ATOMIZER_VERSION = "0.1.0"
GLOSSARY_NORMALIZATION_VERSION = "0.1.0"
SEMANTIC_RETRY_RESET_LIMIT = 2

LOCAL_ALIGNER_REPO = "Qwen/Qwen3-ForcedAligner-0.6B"
LOCAL_ALIGNER_REVISION = "ff5efe6a75df02f6d1d05ac939a673f7909b1849"
CLOUD_MODEL = "qwen3.5-omni-plus-2026-03-15"
REFERENCE_VISION_MODEL = "qwen3.7-plus"
REFERENCE_VISION_HEIGHT = 480
REFERENCE_VISION_FPS = 4
REFERENCE_VISION_JPEG_QV = 8
REFERENCE_VISION_WINDOW_MS = 30_000
CLOUD_REFERENCE_ASR_MODEL = "qwen-audio-3.0-asr-flash"
CLOUD_DOCUMENT_MODEL = "qwen-doc-turbo"
LEXICON_MODEL = REFERENCE_VISION_MODEL
LEXICON_BATCH_MAX_CHARACTERS = 60_000
LEXICON_MODEL_SENT_ATTEMPT_LIMIT = 2
REFERENCE_ASR_SEGMENT_MAX_MS = 225_000
REFERENCE_MODEL_SENT_ATTEMPT_LIMIT = 2
REFERENCE_AUDIO_SAMPLE_RATE_HZ = 16_000
REFERENCE_AUDIO_CHANNELS = 1
REFERENCE_DOCUMENT_POLL_INTERVAL_SECONDS = 2.0
REFERENCE_DOCUMENT_POLL_TIMEOUT_SECONDS = 300.0
ALIGNER_LANGUAGES = (
    "Chinese",
    "English",
    "Cantonese",
    "French",
    "German",
    "Italian",
    "Japanese",
    "Korean",
    "Portuguese",
    "Russian",
    "Spanish",
)


@dataclass(frozen=True)
class MediaPrepConfig:
    version: str = "0.1.0"
    sample_rate_hz: int = 16_000
    channels: int = 1
    sample_format: str = "s16le"
    opening_scan_limit_ms: int = 50_000


@dataclass(frozen=True)
class ChunkerConfig:
    version: str = "0.1.0"
    target_duration_ms: int = 180_000
    hard_limit_ms: int = 225_000
    silence_min_duration_ms: int = 500
    silence_threshold_db: int = -40


@dataclass(frozen=True)
class SegmenterConfig:
    version: str = "0.1.0"
    max_display_units: int = 10
    display_units: tuple[tuple[str, int], ...] = (
        ("cjk_character", 1),
        ("word", 1),
        ("number", 1),
        ("pronounceable_symbol", 1),
    )
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
    version: str = "0.1.1"
    semantic_attempt_limit: int = 4
    alignment_structural_repair_limit: int = 1
    qa_alignment_repair_wave_limit: int = 1
    rework_rule_codes: tuple[str, ...] = (
        "glossary_single_atom_conflict",
        "provider_marked_uncertain",
    )


@dataclass(frozen=True)
class RuntimeDeviceConfig:
    device: str
    dtype: str

    @classmethod
    def detect(cls) -> RuntimeDeviceConfig:
        try:
            torch = import_module("torch")
        except ImportError:
            return cls(device="cpu", dtype="float32")
        if torch.cuda.is_available():
            dtype = "bfloat16" if torch.cuda.is_bf16_supported() else "float16"
            return cls(device="cuda:0", dtype=dtype)
        return cls(device="cpu", dtype="float32")


@dataclass(frozen=True)
class RuntimeConfig:
    ffmpeg: str
    ffprobe: str
    model_cache: str | None
    device: RuntimeDeviceConfig

    @classmethod
    def detect(cls) -> RuntimeConfig:
        ffmpeg = os.getenv("CUEFLOW_FFMPEG") or shutil.which("ffmpeg") or ""
        ffprobe = os.getenv("CUEFLOW_FFPROBE") or shutil.which("ffprobe") or ""
        return cls(
            ffmpeg=ffmpeg,
            ffprobe=ffprobe,
            model_cache=os.getenv("CUEFLOW_MODEL_CACHE"),
            device=RuntimeDeviceConfig.detect(),
        )


def result_config(runtime: RuntimeConfig | None = None) -> dict[str, Any]:
    chosen_runtime = runtime or RuntimeConfig.detect()
    return {
        "media": asdict(MediaPrepConfig()),
        "chunker": asdict(ChunkerConfig()),
        "segmenter": asdict(SegmenterConfig()),
        "qa": asdict(QaRulesetConfig()),
        "runtime_device": asdict(chosen_runtime.device),
    }
