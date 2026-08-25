from __future__ import annotations

import hashlib
import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from cueflow.config import (
    REFERENCE_ASR_SEGMENT_MAX_MS,
    REFERENCE_AUDIO_CHANNELS,
    REFERENCE_AUDIO_SAMPLE_RATE_HZ,
    REFERENCE_VISION_FPS,
    REFERENCE_VISION_HEIGHT,
    REFERENCE_VISION_JPEG_QV,
    REFERENCE_VISION_WINDOW_MS,
    RuntimeConfig,
)
from cueflow.errors import ContractError, ProviderUnavailableError, UnsupportedReferenceError
from cueflow.reference_documents import DocumentExtraction, extract_text_cues

TEXT_SUBTITLE_CODECS = frozenset({"subrip", "webvtt", "ass", "ssa", "mov_text", "text"})
BITMAP_SUBTITLE_CODECS = frozenset({"hdmv_pgs_subtitle", "dvd_subtitle"})


@dataclass(frozen=True)
class ReferenceMediaProbe:
    detected_format: str
    local_measured_duration_ms: int
    width: int | None
    height: int | None
    audio_stream_indices: tuple[int, ...]
    subtitle_streams: tuple[dict[str, Any], ...]
    raw: dict[str, Any]

    @property
    def has_audio(self) -> bool:
        return bool(self.audio_stream_indices)


@dataclass(frozen=True)
class ReferenceWorkSpec:
    branch: str
    evidence_role: str
    kind: str
    config: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {
            "branch": self.branch,
            "evidence_role": self.evidence_role,
            "kind": self.kind,
            "config": dict(self.config),
        }


@dataclass(frozen=True)
class FrameRecord:
    frame_id: str
    source_timestamp_ms: int
    encoded_sha256: str
    path: Path


@dataclass(frozen=True)
class FrameWindow:
    start_ms: int
    end_ms: int
    frames: tuple[FrameRecord, ...]
    command: tuple[str, ...]


@dataclass(frozen=True)
class BitmapOccurrence:
    start_ms: int
    end_ms: int
    stream_index: int
    packet_index: int


@dataclass(frozen=True)
class BitmapPayload:
    raw_pixel_sha256: str
    width: int
    height: int
    path: Path
    occurrences: tuple[BitmapOccurrence, ...]


@dataclass(frozen=True)
class BitmapCueSet:
    codec: str
    unique_bitmaps: tuple[BitmapPayload, ...]
    skipped_empty_count: int
    command_profile: dict[str, Any]


def probe_reference_media(path: Path, runtime: RuntimeConfig) -> ReferenceMediaProbe:
    value = _run_json(
        [
            _required_tool(runtime.ffprobe, "ffprobe"),
            "-v",
            "error",
            "-show_format",
            "-show_streams",
            "-of",
            "json",
            str(path),
        ]
    )
    streams = value.get("streams")
    if not isinstance(streams, list):
        raise UnsupportedReferenceError("FFprobe returned no Reference streams")
    format_value = value.get("format")
    format_map = format_value if isinstance(format_value, dict) else {}
    duration_streams = [
        stream
        for stream in streams
        if isinstance(stream, dict) and stream.get("codec_type") in {"audio", "video"}
    ]
    duration_ms = _duration_ms(format_map.get("duration"), duration_streams)
    video_stream = next(
        (
            stream
            for stream in streams
            if isinstance(stream, dict) and stream.get("codec_type") == "video"
        ),
        None,
    )
    width = _optional_positive_int(video_stream.get("width")) if video_stream else None
    height = _optional_positive_int(video_stream.get("height")) if video_stream else None
    audio_indices = tuple(
        int(stream["index"])
        for stream in streams
        if isinstance(stream, dict)
        and stream.get("codec_type") == "audio"
        and isinstance(stream.get("index"), int)
    )
    subtitle_streams = tuple(
        dict(stream)
        for stream in streams
        if isinstance(stream, dict) and stream.get("codec_type") == "subtitle"
    )
    format_name = str(format_map.get("format_name") or "unknown").split(",", 1)[0]
    return ReferenceMediaProbe(
        detected_format=format_name,
        local_measured_duration_ms=duration_ms,
        width=width,
        height=height,
        audio_stream_indices=audio_indices,
        subtitle_streams=subtitle_streams,
        raw=value,
    )


def plan_reference_media_work(
    probe: ReferenceMediaProbe,
    processing_profile: str,
    pixel_subtitle_mode: Literal["burned", "none"] | None,
) -> tuple[ReferenceWorkSpec, ...]:
    if processing_profile not in {"LOCAL_PROFILE", "CLOUD_PROFILE"}:
        raise ContractError("invalid processing profile")
    codecs = [str(stream.get("codec_name") or "") for stream in probe.subtitle_streams]
    unsupported = [
        codec
        for codec in codecs
        if codec not in TEXT_SUBTITLE_CODECS and codec not in BITMAP_SUBTITLE_CODECS
    ]
    if unsupported:
        raise UnsupportedReferenceError(
            f"unsupported subtitle codec(s): {sorted(set(unsupported))}; "
            "CueFlow will not downgrade to no-subtitle"
        )
    text_streams = [
        stream
        for stream in probe.subtitle_streams
        if str(stream.get("codec_name")) in TEXT_SUBTITLE_CODECS
    ]
    if text_streams:
        return tuple(
            ReferenceWorkSpec(
                branch="text_subtitle",
                evidence_role="text_subtitle",
                kind="text_subtitle",
                config={
                    "stream_index": int(stream["index"]),
                    "codec": str(stream["codec_name"]),
                },
            )
            for stream in text_streams
        )
    bitmap_streams = [
        stream
        for stream in probe.subtitle_streams
        if str(stream.get("codec_name")) in BITMAP_SUBTITLE_CODECS
    ]
    if bitmap_streams:
        if processing_profile == "CLOUD_PROFILE":
            return tuple(
                ReferenceWorkSpec(
                    branch="bitmap_subtitle",
                    evidence_role="bitmap_subtitle",
                    kind="bitmap_vision",
                    config={
                        "stream_index": int(stream["index"]),
                        "codec": str(stream["codec_name"]),
                    },
                )
                for stream in bitmap_streams
            )
        return _asr_specs(probe, cloud=False)

    if probe.width is None or probe.height is None:
        return _asr_specs(probe, cloud=processing_profile == "CLOUD_PROFILE")
    if pixel_subtitle_mode is None:
        raise ContractError(
            "video without an independent subtitle track requires "
            "pixel_subtitle_mode=burned or none"
        )
    if pixel_subtitle_mode not in {"burned", "none"}:
        raise ContractError("pixel_subtitle_mode must be burned or none")
    if processing_profile == "LOCAL_PROFILE":
        return _asr_specs(probe, cloud=False)
    if pixel_subtitle_mode == "none":
        return _asr_specs(probe, cloud=True)

    specs: list[ReferenceWorkSpec] = []
    for start_ms in range(0, probe.local_measured_duration_ms, REFERENCE_VISION_WINDOW_MS):
        end_ms = min(start_ms + REFERENCE_VISION_WINDOW_MS, probe.local_measured_duration_ms)
        specs.append(
            ReferenceWorkSpec(
                branch="burned_subtitle",
                evidence_role="burned_subtitle",
                kind="frame_vision",
                config={"start_ms": start_ms, "end_ms": end_ms},
            )
        )
    specs.extend(_asr_specs(probe, cloud=True))
    return tuple(specs)


def extract_text_subtitle_track(
    path: Path,
    stream_index: int,
    runtime: RuntimeConfig,
    output_dir: Path,
) -> DocumentExtraction:
    output_dir.mkdir(parents=True, exist_ok=True)
    output = output_dir / f"subtitle-{stream_index}.srt"
    _run(
        [
            _required_tool(runtime.ffmpeg, "ffmpeg"),
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(path),
            "-map",
            f"0:{stream_index}",
            "-c:s",
            "srt",
            str(output),
        ]
    )
    return extract_text_cues(output, "srt")


def extract_audio_segment(
    path: Path,
    start_ms: int,
    end_ms: int,
    runtime: RuntimeConfig,
    output: Path,
) -> Path:
    if end_ms <= start_ms or end_ms - start_ms > REFERENCE_ASR_SEGMENT_MAX_MS:
        raise ContractError("Reference ASR segment duration is invalid")
    output.parent.mkdir(parents=True, exist_ok=True)
    _run(
        [
            _required_tool(runtime.ffmpeg, "ffmpeg"),
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-ss",
            _seconds(start_ms),
            "-t",
            _seconds(end_ms - start_ms),
            "-i",
            str(path),
            "-vn",
            "-ac",
            str(REFERENCE_AUDIO_CHANNELS),
            "-ar",
            str(REFERENCE_AUDIO_SAMPLE_RATE_HZ),
            "-c:a",
            "pcm_s16le",
            "-f",
            "wav",
            str(output),
        ]
    )
    if not output.is_file() or output.stat().st_size <= 44:
        raise UnsupportedReferenceError("Reference ASR segment produced no PCM audio")
    return output


def extract_frame_window(
    path: Path,
    start_ms: int,
    end_ms: int,
    runtime: RuntimeConfig,
    output_dir: Path,
) -> FrameWindow:
    if end_ms <= start_ms or end_ms - start_ms > REFERENCE_VISION_WINDOW_MS:
        raise ContractError("Reference Vision window duration is invalid")
    output_dir.mkdir(parents=True, exist_ok=True)
    pattern = output_dir / "frame_%06d.jpg"
    command = [
        _required_tool(runtime.ffmpeg, "ffmpeg"),
        "-hide_banner",
        "-loglevel",
        "info",
        "-y",
        "-ss",
        _seconds(start_ms),
        "-t",
        _seconds(end_ms - start_ms),
        "-i",
        str(path),
        "-map",
        "0:v:0",
        "-vf",
        f"fps={REFERENCE_VISION_FPS},scale=-2:{REFERENCE_VISION_HEIGHT}:flags=lanczos,showinfo",
        "-q:v",
        str(REFERENCE_VISION_JPEG_QV),
        str(pattern),
    ]
    completed = _run(command)
    pts_values = [
        float(value)
        for value in re.findall(r"\bpts_time:([-+]?\d+(?:\.\d+)?)", completed.stderr)
    ]
    paths = sorted(output_dir.glob("frame_*.jpg"))
    if not paths or len(paths) != len(pts_values):
        raise UnsupportedReferenceError("FFmpeg frame manifest does not match saved frames")
    frames = tuple(
        FrameRecord(
            frame_id=f"frame_{index:06d}",
            source_timestamp_ms=start_ms + round(pts_seconds * 1000),
            encoded_sha256=_file_sha256(frame_path),
            path=frame_path,
        )
        for index, (frame_path, pts_seconds) in enumerate(zip(paths, pts_values, strict=True), 1)
    )
    return FrameWindow(start_ms, end_ms, frames, tuple(command))


def prepare_visual_image(
    path: Path,
    runtime: RuntimeConfig,
    output: Path,
) -> tuple[Path, tuple[str, ...], str]:
    output.parent.mkdir(parents=True, exist_ok=True)
    command = [
        _required_tool(runtime.ffmpeg, "ffmpeg"),
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(path),
        "-frames:v",
        "1",
        "-vf",
        f"scale=-2:{REFERENCE_VISION_HEIGHT}:flags=lanczos",
        "-q:v",
        str(REFERENCE_VISION_JPEG_QV),
        str(output),
    ]
    _run(command)
    if not output.is_file():
        raise UnsupportedReferenceError("Reference image conversion produced no JPEG")
    return output, tuple(command), _file_sha256(output)


def extract_bitmap_cues(
    path: Path,
    stream_index: int,
    codec: str,
    probe: ReferenceMediaProbe,
    runtime: RuntimeConfig,
    output_dir: Path,
) -> BitmapCueSet:
    if codec not in BITMAP_SUBTITLE_CODECS:
        raise ContractError("bitmap extraction requires PGS or VobSub")
    if probe.width is None or probe.height is None:
        raise UnsupportedReferenceError("bitmap subtitle extraction requires a video canvas")
    packets_value = _run_json(
        [
            _required_tool(runtime.ffprobe, "ffprobe"),
            "-v",
            "error",
            "-show_packets",
            "-select_streams",
            f"s:{_subtitle_ordinal(probe, stream_index)}",
            "-of",
            "json",
            str(path),
        ]
    )
    raw_packets = packets_value.get("packets")
    if not isinstance(raw_packets, list) or not raw_packets:
        raise UnsupportedReferenceError("bitmap subtitle stream contains no packets")
    packets = _packet_intervals(raw_packets, probe.local_measured_duration_ms, codec)
    subtitle_stream = next(
        (stream for stream in probe.subtitle_streams if stream.get("index") == stream_index),
        None,
    )
    if subtitle_stream is None:
        raise ContractError(f"unknown Reference subtitle stream: {stream_index}")
    canvas_width = _optional_positive_int(subtitle_stream.get("width")) or probe.width
    canvas_height = _optional_positive_int(subtitle_stream.get("height")) or probe.height
    subtitle_ordinal = _subtitle_ordinal(probe, stream_index)
    output_dir.mkdir(parents=True, exist_ok=True)
    grouped: dict[str, dict[str, Any]] = {}
    skipped_empty = 0
    for packet_index, (start_ms, end_ms) in enumerate(packets):
        sample_ms = min(start_ms + 100, max(start_ms, end_ms - 1))
        raw_frame = _render_bitmap_sample(
            path,
            subtitle_ordinal,
            sample_ms,
            canvas_width,
            canvas_height,
            runtime,
        )
        crop = _crop_non_black(raw_frame, canvas_width, canvas_height)
        if crop is None:
            skipped_empty += 1
            continue
        width, height, pixels = crop
        digest = hashlib.sha256(
            width.to_bytes(4, "big") + height.to_bytes(4, "big") + pixels
        ).hexdigest()
        occurrence = BitmapOccurrence(start_ms, end_ms, stream_index, packet_index)
        existing = grouped.get(digest)
        if existing is None:
            png_path = output_dir / f"bitmap-{len(grouped):06d}.png"
            _encode_rgb_png(pixels, width, height, runtime, png_path)
            grouped[digest] = {
                "width": width,
                "height": height,
                "path": png_path,
                "occurrences": [occurrence],
            }
        else:
            existing["occurrences"].append(occurrence)
    if not grouped:
        raise UnsupportedReferenceError("bitmap subtitle stream contains only clear or empty cues")
    bitmaps = tuple(
        BitmapPayload(
            raw_pixel_sha256=digest,
            width=int(value["width"]),
            height=int(value["height"]),
            path=Path(value["path"]),
            occurrences=tuple(value["occurrences"]),
        )
        for digest, value in grouped.items()
    )
    return BitmapCueSet(
        codec=codec,
        unique_bitmaps=bitmaps,
        skipped_empty_count=skipped_empty,
        command_profile={
            "sample_offset_ms": 100,
            "pixel_format": "rgb24",
            "subtitle_canvas": {"width": canvas_width, "height": canvas_height},
            "subtitle_ordinal": subtitle_ordinal,
            "decode_filter": "subtitle scale -> black canvas overlay",
        },
    )


def _asr_specs(probe: ReferenceMediaProbe, *, cloud: bool) -> tuple[ReferenceWorkSpec, ...]:
    role = "cloud_reference_asr" if cloud else "local_reference_asr"
    branch = role
    if not probe.has_audio:
        return (
            ReferenceWorkSpec(
                branch=branch,
                evidence_role=role,
                kind="asr_unavailable",
                config={"reason": "Reference has no audio stream"},
            ),
        )
    return tuple(
        ReferenceWorkSpec(
            branch=branch,
            evidence_role=role,
            kind="asr",
            config={
                "start_ms": start_ms,
                "end_ms": min(
                    start_ms + REFERENCE_ASR_SEGMENT_MAX_MS,
                    probe.local_measured_duration_ms,
                ),
            },
        )
        for start_ms in range(0, probe.local_measured_duration_ms, REFERENCE_ASR_SEGMENT_MAX_MS)
    )


def _packet_intervals(
    packets: list[Any], local_duration_ms: int, codec: str
) -> list[tuple[int, int]]:
    starts: list[int] = []
    durations: list[int | None] = []
    for raw in packets:
        if not isinstance(raw, dict) or raw.get("pts_time") is None:
            continue
        try:
            start_ms = round(float(raw["pts_time"]) * 1000)
            duration_ms = (
                round(float(raw["duration_time"]) * 1000)
                if raw.get("duration_time") is not None
                else None
            )
        except (TypeError, ValueError) as exc:
            raise UnsupportedReferenceError("bitmap subtitle packet timing is invalid") from exc
        if duration_ms is not None and duration_ms > local_duration_ms + 1000:
            message = "VobSub" if codec == "dvd_subtitle" else "bitmap subtitle"
            raise UnsupportedReferenceError(
                f"{message} timing gate failed: packet duration {duration_ms}ms exceeds media"
            )
        starts.append(max(0, start_ms))
        durations.append(duration_ms)
    if not starts:
        raise UnsupportedReferenceError("bitmap subtitle packets contain no usable timing")
    intervals: list[tuple[int, int]] = []
    for index, start_ms in enumerate(starts):
        next_start = starts[index + 1] if index + 1 < len(starts) else local_duration_ms
        duration_ms = durations[index]
        end_ms = min(
            local_duration_ms,
            start_ms + duration_ms if duration_ms is not None and duration_ms > 0 else next_start,
        )
        if end_ms > start_ms:
            intervals.append((start_ms, end_ms))
    return intervals


def _render_bitmap_sample(
    path: Path,
    subtitle_ordinal: int,
    sample_ms: int,
    width: int,
    height: int,
    runtime: RuntimeConfig,
) -> bytes:
    command = [
        _required_tool(runtime.ffmpeg, "ffmpeg"),
        "-hide_banner",
        "-loglevel",
        "error",
        "-f",
        "lavfi",
        "-i",
        f"color=c=black:s={width}x{height}:r=10",
        "-i",
        str(path),
        "-filter_complex",
        (
            f"[1:s:{subtitle_ordinal}]scale={width}:{height}[sub];"
            "[0:v:0][sub]overlay,format=rgb24[outv]"
        ),
        "-map",
        "[outv]",
        "-ss",
        _seconds(sample_ms),
        "-frames:v",
        "1",
        "-f",
        "rawvideo",
        "pipe:1",
    ]
    completed = _run(command, text=False)
    raw = completed.stdout
    if not isinstance(raw, bytes) or len(raw) != width * height * 3:
        raise UnsupportedReferenceError("bitmap subtitle decoder returned an invalid RGB frame")
    return raw


def _crop_non_black(raw: bytes, width: int, height: int) -> tuple[int, int, bytes] | None:
    left = width
    top = height
    right = -1
    bottom = -1
    for y in range(height):
        row_offset = y * width * 3
        for x in range(width):
            offset = row_offset + x * 3
            if raw[offset] or raw[offset + 1] or raw[offset + 2]:
                left = min(left, x)
                top = min(top, y)
                right = max(right, x)
                bottom = max(bottom, y)
    if right < left or bottom < top:
        return None
    crop_width = right - left + 1
    crop_height = bottom - top + 1
    rows = [
        raw[((top + y) * width + left) * 3 : ((top + y) * width + right + 1) * 3]
        for y in range(crop_height)
    ]
    return crop_width, crop_height, b"".join(rows)


def _encode_rgb_png(
    raw: bytes,
    width: int,
    height: int,
    runtime: RuntimeConfig,
    output: Path,
) -> None:
    command = [
        _required_tool(runtime.ffmpeg, "ffmpeg"),
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-f",
        "rawvideo",
        "-pix_fmt",
        "rgb24",
        "-s",
        f"{width}x{height}",
        "-i",
        "pipe:0",
        "-frames:v",
        "1",
        str(output),
    ]
    _run(command, input_bytes=raw, text=False)


def _subtitle_ordinal(probe: ReferenceMediaProbe, absolute_index: int) -> int:
    for ordinal, stream in enumerate(probe.subtitle_streams):
        if stream.get("index") == absolute_index:
            return ordinal
    raise ContractError(f"unknown Reference subtitle stream: {absolute_index}")


def _duration_ms(format_value: Any, streams: list[Any]) -> int:
    candidates: list[float] = []
    values = [
        format_value,
        *(stream.get("duration") for stream in streams if isinstance(stream, dict)),
    ]
    for value in values:
        if value is None:
            continue
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            continue
        if parsed > 0:
            candidates.append(parsed)
    if not candidates:
        raise UnsupportedReferenceError("Reference media has no reliable local duration")
    return round(max(candidates) * 1000)


def _optional_positive_int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None


def _required_tool(value: str, name: str) -> str:
    if not value:
        raise ProviderUnavailableError(f"{name} is required for Reference media")
    return value


def _run_json(command: list[str]) -> dict[str, Any]:
    completed = _run(command)
    try:
        value = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise UnsupportedReferenceError("media tool returned invalid JSON") from exc
    if not isinstance(value, dict):
        raise UnsupportedReferenceError("media tool returned invalid JSON")
    return value


def _run(
    command: list[str],
    *,
    input_bytes: bytes | None = None,
    text: bool = True,
) -> subprocess.CompletedProcess[Any]:
    try:
        completed = subprocess.run(
            command,
            input=input_bytes,
            check=False,
            capture_output=True,
            text=text,
        )
    except OSError as exc:
        raise ProviderUnavailableError(f"media tool unavailable: {command[0]}") from exc
    if completed.returncode != 0:
        stderr = (
            completed.stderr.decode(errors="replace")
            if isinstance(completed.stderr, bytes)
            else completed.stderr
        )
        raise UnsupportedReferenceError("media tool failed: " + str(stderr).strip())
    return completed


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _seconds(milliseconds: int) -> str:
    return f"{milliseconds / 1000:.3f}"
