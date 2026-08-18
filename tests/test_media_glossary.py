from __future__ import annotations

import math
import wave
from array import array
from pathlib import Path

from cueflow.atomizer import atomize
from cueflow.config import ChunkerConfig
from cueflow.glossary import exact_protected_spans, normalize_terms
from cueflow.media import build_chunk_plan, detect_silence_spans, slice_wave


def _write_wave(path: Path, regions: list[tuple[int, int]]) -> None:
    samples = array("h")
    for duration_ms, amplitude in regions:
        for index in range(duration_ms * 16):
            value = 0 if amplitude == 0 else round(amplitude * math.sin(index / 8))
            samples.append(value)
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(16_000)
        output.writeframes(samples.tobytes())


def test_chunker_uses_nearest_qualified_silence_and_covers_timeline(tmp_path: Path) -> None:
    timeline = tmp_path / "timeline.wav"
    _write_wave(timeline, [(1000, 5000), (600, 0), (1000, 5000)])
    config = ChunkerConfig(
        target_duration_ms=1200,
        hard_limit_ms=2000,
        silence_min_duration_ms=500,
    )
    silences = detect_silence_spans(timeline, config)
    assert silences == [(1000, 1600)]
    chunks = build_chunk_plan(2600, silences, config)
    assert chunks == [
        {
            "chunk_id": "chunk_0001",
            "ordinal": 0,
            "global_start_ms": 0,
            "global_end_ms": 1300,
        },
        {
            "chunk_id": "chunk_0002",
            "ordinal": 1,
            "global_start_ms": 1300,
            "global_end_ms": 2600,
        },
    ]


def test_wave_slice_preserves_exact_timeline_interval(tmp_path: Path) -> None:
    source = tmp_path / "source.wav"
    destination = tmp_path / "slice.wav"
    _write_wave(source, [(2000, 5000)])
    slice_wave(source, destination, 250, 1750)
    with wave.open(str(destination), "rb") as sliced:
        assert sliced.getframerate() == 16_000
        assert sliced.getnframes() == 24_000


def test_glossary_normalization_and_multi_atom_protection() -> None:
    assert normalize_terms(["  Cafe\u0301 ", "Café", "术语", "术语"]) == ["Café", "术语"]
    _, atoms = atomize("学习卡尔曼滤波方法")
    spans = exact_protected_spans(atoms, ["卡尔曼滤波", "卡"])
    assert spans == [(2, 7, "卡尔曼滤波")]
