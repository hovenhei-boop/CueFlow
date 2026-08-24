from __future__ import annotations

import os
import shutil
from dataclasses import asdict, dataclass
from importlib import import_module
from typing import Any

SCHEMA_VERSION = "1.0.0"
COMPONENT_VERSION = "0.1.1"
ATOMIZER_VERSION = "0.1.0"
GLOSSARY_NORMALIZATION_VERSION = "0.1.0"
SEMANTIC_RETRY_RESET_LIMIT = 2

LOCAL_ASR_REPO = "Qwen/Qwen3-ASR-1.7B"
LOCAL_ASR_REVISION = "e6942fcb56f665d470e39e6fe9efe6f5f31ee254"
LOCAL_ALIGNER_REPO = "Qwen/Qwen3-ForcedAligner-0.6B"
LOCAL_ALIGNER_REVISION = "ff5efe6a75df02f6d1d05ac939a673f7909b1849"
CLOUD_MODEL = "qwen3.5-omni-plus-2026-03-15"
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
class ProfileConfig:
    name: str
    semantic_provider: str
    semantic_model: str
    semantic_revision: str
    aligner_provider: str = "qwen-local"
    aligner_model: str = LOCAL_ALIGNER_REPO
    aligner_revision: str = LOCAL_ALIGNER_REVISION


PROFILES: dict[str, ProfileConfig] = {
    "LOCAL_PROFILE": ProfileConfig(
        name="LOCAL_PROFILE",
        semantic_provider="qwen-local",
        semantic_model=LOCAL_ASR_REPO,
        semantic_revision=LOCAL_ASR_REVISION,
    ),
    "CLOUD_PROFILE": ProfileConfig(
        name="CLOUD_PROFILE",
        semantic_provider="dashscope-openai-compatible",
        semantic_model=CLOUD_MODEL,
        semantic_revision=CLOUD_MODEL,
    ),
}


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


def result_config(profile: str, runtime: RuntimeConfig | None = None) -> dict[str, Any]:
    chosen_runtime = runtime or RuntimeConfig.detect()
    return {
        "profile": asdict(PROFILES[profile]),
        "media": asdict(MediaPrepConfig()),
        "chunker": asdict(ChunkerConfig()),
        "segmenter": asdict(SegmenterConfig()),
        "qa": asdict(QaRulesetConfig()),
        "runtime_device": asdict(chosen_runtime.device),
    }
