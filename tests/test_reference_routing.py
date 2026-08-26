from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

import cueflow.reference_media as reference_media
from cueflow.config import RuntimeConfig, RuntimeDeviceConfig
from cueflow.errors import ContractError, UnsupportedReferenceError
from cueflow.reference_media import (
    ReferenceMediaProbe,
    extract_audio_segment,
    extract_bitmap_cues,
    extract_frame_window,
    plan_reference_media_work,
)
from cueflow.schema import REFERENCE_EVIDENCE_ROLES


def _probe(
    *,
    subtitles: tuple[dict[str, Any], ...] = (),
    audio: bool = True,
    video: bool = True,
    duration_ms: int = 40_054,
) -> ReferenceMediaProbe:
    return ReferenceMediaProbe(
        detected_format="matroska",
        local_measured_duration_ms=duration_ms,
        width=1920 if video else None,
        height=1080 if video else None,
        audio_stream_indices=(1,) if audio else (),
        subtitle_streams=subtitles,
        raw={},
    )


def test_reference_evidence_roles_are_frozen_and_not_fusion_roles() -> None:
    assert REFERENCE_EVIDENCE_ROLES == frozenset(
        {
            "text_subtitle",
            "bitmap_subtitle",
            "burned_subtitle",
            "cloud_reference_asr",
            "document_text",
            "cloud_document_parse",
            "image_visual",
        }
    )
    assert not any("fusion" in role or "glossary" in role for role in REFERENCE_EVIDENCE_ROLES)


def test_frozen_video_routing_matrix_and_negative_routes() -> None:
    text = _probe(subtitles=({"index": 2, "codec_name": "subrip"},))
    specs = plan_reference_media_work(text, None)
    assert [(item.kind, item.evidence_role) for item in specs] == [
        ("text_subtitle", "text_subtitle")
    ]

    bitmap = _probe(
        subtitles=(
            {
                "index": 2,
                "codec_name": "dvd_subtitle",
                "width": 720,
                "height": 576,
            },
        )
    )
    assert [item.kind for item in plan_reference_media_work(bitmap, None)] == [
        "bitmap_vision"
    ]

    burned_cloud = plan_reference_media_work(_probe(), "burned")
    assert {item.kind for item in burned_cloud} == {"frame_vision", "asr"}
    assert {item.evidence_role for item in burned_cloud} == {
        "burned_subtitle",
        "cloud_reference_asr",
    }
    assert [item.kind for item in plan_reference_media_work(_probe(), "none")] == [
        "asr"
    ]

    with pytest.raises(ContractError, match="pixel_subtitle_mode"):
        plan_reference_media_work(_probe(), None)
    with pytest.raises(UnsupportedReferenceError, match="will not downgrade"):
        plan_reference_media_work(
            _probe(subtitles=({"index": 2, "codec_name": "unsupported_codec"},)),
            None,
        )


def test_no_audio_routes_are_explicit_and_burned_cloud_can_be_partial() -> None:
    bitmap_without_audio = _probe(
        subtitles=(
            {
                "index": 2,
                "codec_name": "hdmv_pgs_subtitle",
                "width": 1920,
                "height": 1080,
            },
        ),
        audio=False,
    )
    assert [
        item.kind
        for item in plan_reference_media_work(bitmap_without_audio, None)
    ] == ["bitmap_vision"]

    burned = plan_reference_media_work(
        _probe(audio=False, duration_ms=30_001), "burned"
    )
    assert [item.kind for item in burned] == [
        "frame_vision",
        "frame_vision",
        "asr_unavailable",
    ]


def test_asr_segmentation_is_pcm_work_item_bounded_and_covers_source_timeline() -> None:
    specs = plan_reference_media_work(
        _probe(video=False, duration_ms=450_001), None
    )
    assert [(item.config["start_ms"], item.config["end_ms"]) for item in specs] == [
        (0, 225_000),
        (225_000, 450_000),
        (450_000, 450_001),
    ]
    assert all(item.evidence_role == "cloud_reference_asr" for item in specs)


def test_full_frame_manifest_uses_phase0_ffmpeg_qv8_and_pipeline_timestamps(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: list[str] = []

    def fake_run(
        command: list[str],
        *,
        input_bytes: bytes | None = None,
        text: bool = True,
    ) -> subprocess.CompletedProcess[Any]:
        del input_bytes, text
        captured.extend(command)
        pattern = Path(command[-1])
        (pattern.parent / "frame_000001.jpg").write_bytes(b"encoded JPEG")
        return subprocess.CompletedProcess(command, 0, "", "showinfo pts_time:0.250")

    monkeypatch.setattr(reference_media, "_run", fake_run)
    window = extract_frame_window(
        tmp_path / "video.mkv",
        30_000,
        60_000,
        RuntimeConfig("ffmpeg", "ffprobe", None, RuntimeDeviceConfig("cpu", "float32")),
        tmp_path / "frames",
    )
    assert captured[captured.index("-q:v") + 1] == "8"
    assert "fps=4,scale=-2:480:flags=lanczos,showinfo" in captured
    assert window.frames[0].frame_id == "frame_000001"
    assert window.frames[0].source_timestamp_ms == 30_250


def test_reference_asr_upload_input_is_pcm_wav_without_compression_switch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: list[str] = []

    def fake_run(command: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[Any]:
        captured.extend(command)
        Path(command[-1]).write_bytes(b"R" * 45)
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(reference_media, "_run", fake_run)
    output = extract_audio_segment(
        tmp_path / "video.mkv",
        225_000,
        450_000,
        RuntimeConfig("ffmpeg", "ffprobe", None, RuntimeDeviceConfig("cpu", "float32")),
        tmp_path / "segment.wav",
    )
    assert output.suffix == ".wav"
    assert captured[captured.index("-c:a") + 1] == "pcm_s16le"
    assert captured[captured.index("-f") + 1] == "wav"
    assert "opus" not in captured


def test_vobsub_official_fixture_timing_and_decode_filter_are_supported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Offline contract copy of FFmpeg's independent reddwarf-vobsub sample facts."""
    packets = [
        {"pts_time": start, "duration_time": duration}
        for start, duration in (
            ("0.079", "1.991"),
            ("3.079", "0.990"),
            ("4.200", "1.991"),
            ("7.080", "2.992"),
            ("12.079", "3.994"),
            ("20.079", "1.991"),
            ("26.080", "4.995"),
            ("34.079", "5.120"),
            ("40.080", "0.990"),
            ("41.280", "1.877"),
            ("46.079", "3.000"),
            ("49.079", "2.230"),
            ("52.080", "3.994"),
            ("58.079", "1.991"),
        )
    ]
    monkeypatch.setattr(reference_media, "_run_json", lambda _command: {"packets": packets})
    captured_filters: list[str] = []

    def fake_run(
        command: list[str],
        *,
        input_bytes: bytes | None = None,
        text: bool = True,
    ) -> subprocess.CompletedProcess[Any]:
        del input_bytes
        if "-filter_complex" in command:
            captured_filters.append(command[command.index("-filter_complex") + 1])
            raw = bytearray(720 * 576 * 3)
            raw[-3:] = b"\xff\xff\xff"
            return subprocess.CompletedProcess(command, 0, bytes(raw), b"")
        if command[-1].endswith(".png"):
            Path(command[-1]).write_bytes(b"valid unique bitmap fixture")
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(reference_media, "_run", fake_run)
    probe = _probe(
        subtitles=(
            {
                "index": 3,
                "codec_name": "dvd_subtitle",
                "width": 720,
                "height": 576,
            },
        ),
        duration_ms=60_248,
    )
    result = extract_bitmap_cues(
        tmp_path / "independent-vobsub.mkv",
        3,
        "dvd_subtitle",
        probe,
        RuntimeConfig("ffmpeg", "ffprobe", None, RuntimeDeviceConfig("cpu", "float32")),
        tmp_path / "bitmaps",
    )
    assert len(result.unique_bitmaps) == 1
    assert len(result.unique_bitmaps[0].occurrences) == 14
    assert result.command_profile["subtitle_canvas"] == {"width": 720, "height": 576}
    assert captured_filters == [
        "[1:s:0]scale=720:576[sub];[0:v:0][sub]overlay,format=rgb24[outv]"
    ] * 14


def test_vobsub_abnormal_duration_stops_before_claiming_support(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        reference_media,
        "_run_json",
        lambda _command: {
            "packets": [{"pts_time": "0.1", "duration_time": "3932159.999"}]
        },
    )
    probe = _probe(
        subtitles=(
            {
                "index": 3,
                "codec_name": "dvd_subtitle",
                "width": 720,
                "height": 576,
            },
        ),
        duration_ms=10_000,
    )
    with pytest.raises(UnsupportedReferenceError, match="VobSub timing gate failed"):
        extract_bitmap_cues(
            tmp_path / "abnormal-vobsub.mkv",
            3,
            "dvd_subtitle",
            probe,
            RuntimeConfig(
                "ffmpeg", "ffprobe", None, RuntimeDeviceConfig("cpu", "float32")
            ),
            tmp_path / "bitmaps",
        )
