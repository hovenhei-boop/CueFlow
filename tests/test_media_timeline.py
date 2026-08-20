from __future__ import annotations

import io
from copy import deepcopy
from fractions import Fraction
from pathlib import Path
from typing import Any

import pytest

import cueflow.media as media_module
from cueflow.config import RuntimeConfig, RuntimeDeviceConfig
from cueflow.errors import ContractError
from cueflow.media import (
    analyze_presentation_timeline,
    build_timeline_actions,
    scan_packet_continuity,
    timeline_filters_from_actions,
)
from cueflow.schema import validate_payload


def _metadata(*, video_duration_ticks: int = 4_000) -> dict[str, Any]:
    return {
        "streams": [
            {
                "index": 0,
                "codec_type": "video",
                "codec_name": "h264",
                "time_base": "1/1000",
                "duration_ts": video_duration_ticks,
            },
            {
                "index": 1,
                "codec_type": "audio",
                "codec_name": "aac",
                "time_base": "1/48000",
                "sample_rate": "48000",
                "duration_ts": video_duration_ticks * 48,
            },
        ],
        "format": {"format_name": "mov,mp4", "duration": "4.000"},
    }


def _opening(*, video_pts_ms: int = 0, audio_pts: int | None = 0, skip: int = 0) -> dict[str, Any]:
    frames: list[dict[str, Any]] = [
        {"stream_index": 0, "pts": video_pts_ms, "best_effort_timestamp": video_pts_ms}
    ]
    if audio_pts is not None:
        frames.append(
            {
                "stream_index": 1,
                "pts": audio_pts,
                "best_effort_timestamp": audio_pts,
                "nb_samples": 1024,
                "sample_rate": 48000,
                "side_data_list": [{"skip_samples": skip}],
            }
        )
    return {"frames": frames}


def _continuous() -> dict[str, Any]:
    return {"status": "continuous", "packets_scanned": 2, "first_anomaly": None}


@pytest.mark.parametrize(
    ("offset", "expected_action", "expected_samples", "expected_status"),
    [
        (Fraction(0), "timeline_origin_unchanged", None, "normal"),
        (Fraction(13, 1000), "pad_silence_before", 208, "corrected"),
        (Fraction(437, 1000), "pad_silence_before", 6992, "corrected"),
        (Fraction(-21, 1000), "trim_before_timeline", 336, "corrected"),
    ],
)
def test_every_reliable_offset_is_quantized_once_to_timeline_samples(
    offset: Fraction,
    expected_action: str,
    expected_samples: int | None,
    expected_status: str,
) -> None:
    audio_pts = offset * 48_000
    assert audio_pts.denominator == 1
    result = analyze_presentation_timeline(
        _metadata(),
        _opening(audio_pts=audio_pts.numerator),
        _continuous(),
    )
    origin = result.payload["timeline_actions"][0]
    assert origin["action"] == expected_action
    assert origin.get("sample_count") == expected_samples
    assert result.payload["timeline_status"] == expected_status
    assert result.payload["presentation_evidence"]["exact_offset"] == {
        "numerator": offset.numerator,
        "denominator": offset.denominator,
    }


def test_silent_audio_frames_at_zero_are_not_confused_with_first_audible_sound() -> None:
    opening = _opening(audio_pts=0)
    opening["frames"].append(
        {
            "stream_index": 1,
            "pts": 45 * 48_000,
            "best_effort_timestamp": 45 * 48_000,
            "nb_samples": 1024,
            "sample_rate": 48000,
            "fixture_audible": True,
        }
    )
    result = analyze_presentation_timeline(
        _metadata(video_duration_ticks=50_000), opening, _continuous()
    )
    assert result.payload["timeline_actions"][0]["action"] == "timeline_origin_unchanged"


def test_aac_priming_skip_samples_moves_first_valid_sample_to_zero() -> None:
    opening = _opening(audio_pts=-1024, skip=1024)
    opening["frames"].append(
        {
            "stream_index": 1,
            "pts": 0,
            "best_effort_timestamp": 0,
            "nb_samples": 1024,
            "sample_rate": 48000,
        }
    )
    result = analyze_presentation_timeline(
        _metadata(), opening, _continuous()
    )
    assert result.payload["timeline_status"] == "normal"
    assert result.payload["timeline_actions"][0]["action"] == "timeline_origin_unchanged"
    assert result.payload["presentation_evidence"]["audio_start"]["pts"] == 0


def test_negative_pts_and_edit_list_origins_use_presentation_timestamps() -> None:
    negative = analyze_presentation_timeline(
        _metadata(), _opening(audio_pts=-1008), _continuous()
    )
    assert negative.payload["timeline_actions"][0] == {
        "action": "trim_before_timeline",
        "sample_count": 336,
    }

    edit_list = analyze_presentation_timeline(
        _metadata(), _opening(video_pts_ms=500, audio_pts=24_000), _continuous()
    )
    assert edit_list.payload["timeline_actions"][0]["action"] == "timeline_origin_unchanged"
    assert edit_list.payload["presentation_evidence"]["media_origin"]["pts"] == 500


def test_missing_reliable_opening_timestamp_is_unverified() -> None:
    result = analyze_presentation_timeline(
        _metadata(), _opening(audio_pts=None), _continuous()
    )
    assert result.payload["timeline_status"] == "unverified"
    assert result.payload["timeline_actions"][0]["action"] == "timeline_origin_unverified"
    assert "opening_presentation_timestamp_unavailable" in result.payload["timeline_issues"]


def test_media_probe_schema_accepts_nullable_production_evidence() -> None:
    result = analyze_presentation_timeline(
        _metadata(),
        _opening(audio_pts=None),
        {
            "status": "unavailable",
            "packets_scanned": 0,
            "first_anomaly": {"code": "audio_packet_timeline_unavailable"},
        },
    )
    validate_payload("media_probe", result.payload)


def test_media_probe_schema_rejects_invalid_raw_timing_evidence() -> None:
    payload = analyze_presentation_timeline(
        _metadata(), _opening(audio_pts=-1008), _continuous()
    ).payload
    validate_payload("media_probe", payload)

    invalid_pts = deepcopy(payload)
    invalid_pts["presentation_evidence"]["audio_start"]["pts"] = "-1008"
    with pytest.raises(ContractError, match="audio_start.pts must be an integer"):
        validate_payload("media_probe", invalid_pts)

    invalid_time_base = deepcopy(payload)
    invalid_time_base["audio_stream"]["time_base_den"] = 0
    with pytest.raises(ContractError, match="audio_stream.time_base_den must be positive"):
        validate_payload("media_probe", invalid_time_base)

    invalid_time_base_numerator = deepcopy(payload)
    invalid_time_base_numerator["audio_stream"]["time_base_num"] = "1"
    with pytest.raises(ContractError, match="audio_stream.time_base_num must be an integer"):
        validate_payload("media_probe", invalid_time_base_numerator)

    invalid_count = deepcopy(payload)
    invalid_count["continuity_check"]["packets_scanned"] = -1
    with pytest.raises(ContractError, match="packets_scanned must be non-negative"):
        validate_payload("media_probe", invalid_count)

    invalid_anomaly = deepcopy(payload)
    invalid_anomaly["continuity_check"]["first_anomaly"] = {"code": "unexpected"}
    with pytest.raises(ContractError, match="must not contain first_anomaly"):
        validate_payload("media_probe", invalid_anomaly)


class _FakeProcess:
    def __init__(self, output: str) -> None:
        self.stdout = io.StringIO(output)
        self.stderr = io.StringIO("")

    def wait(self) -> int:
        return 0


def test_full_file_continuity_scan_detects_a_gap_well_after_opening_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    packet_rows = "0,1024\n1024,1024\n5760000,1024\n"
    monkeypatch.setattr(
        media_module.subprocess,
        "Popen",
        lambda *args, **kwargs: _FakeProcess(packet_rows),
    )
    runtime = RuntimeConfig(
        ffmpeg="ffmpeg",
        ffprobe="ffprobe",
        model_cache=None,
        device=RuntimeDeviceConfig("cpu", "float32"),
    )
    result = scan_packet_continuity(
        Path("fixture.mp4"), runtime, {"time_base": "1/48000"}
    )
    assert result["packets_scanned"] == 3
    assert result["status"] == "discontinuous"
    assert result["first_anomaly"]["code"] == "audio_timestamp_gap"
    assert result["first_anomaly"]["packet_ordinal"] == 2


@pytest.mark.parametrize(
    ("offset", "expected_filter"),
    [
        (Fraction(0), "asetpts=PTS-STARTPTS"),
        (Fraction(13, 1000), "adelay=208S:all=1"),
        (Fraction(-21, 1000), "atrim=start_sample=336"),
    ],
)
def test_timeline_action_is_the_only_render_filter_decision(
    offset: Fraction, expected_filter: str
) -> None:
    actions, _ = build_timeline_actions(offset, total_sample_count=64_000)
    filters = timeline_filters_from_actions(actions)
    assert expected_filter in filters
    assert filters[-2:] == ["apad", "atrim=end_sample=64000"]


def test_unknown_or_incomplete_timeline_actions_fail_closed() -> None:
    with pytest.raises(ContractError, match="unknown timeline action"):
        timeline_filters_from_actions(
            [
                {"action": "invented"},
                {"action": "fit_presentation_duration", "total_sample_count": 16_000},
            ]
        )
    with pytest.raises(ContractError, match="requires one origin"):
        timeline_filters_from_actions(
            [{"action": "timeline_origin_unchanged"}]
        )
