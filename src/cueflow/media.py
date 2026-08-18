from __future__ import annotations

import json
import math
import os
import subprocess
import sys
import tempfile
import wave
from array import array
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, cast

from cueflow.canonical import hash_json
from cueflow.config import COMPONENT_VERSION, ChunkerConfig, MediaPrepConfig, RuntimeConfig
from cueflow.errors import ContractError, ProviderUnavailableError
from cueflow.project import ProjectContext
from cueflow.schema import ArtifactEnvelope, InputRef, Producer


@dataclass(frozen=True)
class ProbeResult:
    media_kind: str
    duration_ms: int
    payload: dict[str, Any]


@dataclass(frozen=True)
class MediaBundle:
    probe: ArtifactEnvelope
    timeline_audio: ArtifactEnvelope
    video_proxy: ArtifactEnvelope | None
    chunk_plan: ArtifactEnvelope
    media_chunks: tuple[ArtifactEnvelope, ...]


def probe_source(path: Path, runtime: RuntimeConfig) -> ProbeResult:
    if not runtime.ffprobe:
        raise ProviderUnavailableError("ffprobe is required for Media Prep")
    command = [
        runtime.ffprobe,
        "-v",
        "error",
        "-show_format",
        "-show_streams",
        "-show_packets",
        "-show_entries",
        "packet=stream_index,pts_time,duration_time,flags",
        "-of",
        "json",
        str(path),
    ]
    raw = _run_json(command)
    streams = raw.get("streams")
    if not isinstance(streams, list):
        raise ContractError("ffprobe returned no streams array")
    stream_items = [cast(dict[str, Any], item) for item in streams if isinstance(item, dict)]
    video = next((item for item in stream_items if item.get("codec_type") == "video"), None)
    audio = next((item for item in stream_items if item.get("codec_type") == "audio"), None)
    if audio is None:
        raise ContractError("CueFlow v0.1 requires an audio stream")
    media_kind = "video" if video is not None else "audio"
    format_value = raw.get("format")
    format_info: Mapping[str, Any] = (
        cast(dict[str, Any], format_value) if isinstance(format_value, dict) else {}
    )
    authority = video if video is not None else audio
    duration_ms = _duration_ms(authority, format_info)
    if duration_ms <= 0:
        raise ContractError("media duration is unavailable or non-positive")
    media_start_ms = _seconds_ms(authority.get("start_time"), default=0)
    audio_start_ms = _seconds_ms(audio.get("start_time"), default=media_start_ms)
    audio_duration_ms = _duration_ms(audio, format_info)
    audio_end_ms = audio_start_ms + audio_duration_ms
    media_end_ms = media_start_ms + duration_ms
    config = MediaPrepConfig()
    issues: list[str] = []
    actions: list[dict[str, Any]] = []
    leading_delta = audio_start_ms - media_start_ms
    trailing_delta = media_end_ms - audio_end_ms
    if leading_delta > config.timeline_tolerance_ms:
        issues.append("audio_starts_after_media")
        actions.append({"action": "pad_silence_before", "duration_ms": leading_delta})
    elif leading_delta < -config.timeline_tolerance_ms:
        issues.append("audio_starts_before_media")
        actions.append({"action": "trim_before_timeline", "duration_ms": -leading_delta})
    if trailing_delta > config.timeline_tolerance_ms:
        issues.append("audio_ends_before_media")
        actions.append({"action": "pad_silence_after", "duration_ms": trailing_delta})
    elif trailing_delta < -config.timeline_tolerance_ms:
        issues.append("audio_ends_after_media")
        actions.append({"action": "trim_after_timeline", "duration_ms": -trailing_delta})
    packet_issue = _packet_discontinuity(
        raw.get("packets"), int(audio.get("index", -1)), config.timeline_tolerance_ms
    )
    if packet_issue:
        issues.append(packet_issue)
    timeline_status = "unverified" if packet_issue else ("corrected" if actions else "normal")
    payload = {
        "media_kind": media_kind,
        "presentation_duration_ms": duration_ms,
        "timeline_status": timeline_status,
        "timeline_tolerance_ms": config.timeline_tolerance_ms,
        "container": {
            "format_name": str(format_info.get("format_name", "unknown")),
            "start_ms": _seconds_ms(format_info.get("start_time"), default=0),
            "duration_ms": _duration_ms(format_info, format_info),
        },
        "video_stream": _stream_facts(video) if video is not None else None,
        "audio_stream": _stream_facts(audio),
        "timeline_issues": issues,
        "timeline_actions": actions,
    }
    return ProbeResult(media_kind=media_kind, duration_ms=duration_ms, payload=payload)


def prepare_media(
    context: ProjectContext,
    source_asset: Mapping[str, Any],
    probe: ProbeResult,
    runtime: RuntimeConfig,
) -> MediaBundle:
    if not runtime.ffmpeg:
        raise ProviderUnavailableError("ffmpeg is required for Media Prep")
    source_path = context.verify_external_asset(str(source_asset["source_asset_id"]))
    media_config = MediaPrepConfig()
    chunk_config = ChunkerConfig()
    media_producer = _producer("media", asdict(media_config))
    chunk_producer = _producer("chunker", asdict(chunk_config))
    source_input = InputRef(
        role="source_media", source_asset_id=str(source_asset["source_asset_id"])
    )
    probe_envelope = ArtifactEnvelope.create(
        artifact_kind="media_probe",
        scope_key="global",
        producer=media_producer,
        inputs=[source_input],
        payload=probe.payload,
    )

    timeline_temp = _temp_path(context, ".wav")
    proxy_temp: Path | None = None
    chunk_temps: list[Path] = []
    try:
        render_timeline_audio(source_path, probe, timeline_temp, runtime)
        audio_hash, audio_length, audio_blob = context.store.publish_blob(timeline_temp)
        timeline_payload = {
            "source_asset_id": str(source_asset["source_asset_id"]),
            "media_probe_artifact_id": probe_envelope.artifact_id,
            "duration_ms": probe.duration_ms,
            "sample_rate_hz": media_config.sample_rate_hz,
            "channels": media_config.channels,
            "sample_format": media_config.sample_format,
            "timeline_origin_ms": 0,
            "audio_blob": _blob(audio_hash, audio_length, "audio/wav"),
        }
        timeline_envelope = ArtifactEnvelope.create(
            artifact_kind="timeline_audio",
            scope_key="global",
            producer=media_producer,
            inputs=[
                source_input,
                InputRef(role="media_probe", artifact_id=probe_envelope.artifact_id),
            ],
            payload=timeline_payload,
        )

        proxy_envelope: ArtifactEnvelope | None = None
        if probe.media_kind == "video":
            proxy_temp = _temp_path(context, ".mp4")
            render_video_proxy(source_path, timeline_temp, probe.duration_ms, proxy_temp, runtime)
            proxy_hash, proxy_length, _ = context.store.publish_blob(proxy_temp)
            width, height = probe_video_size(proxy_temp, runtime)
            proxy_payload = {
                "source_asset_id": str(source_asset["source_asset_id"]),
                "media_probe_artifact_id": probe_envelope.artifact_id,
                "timeline_audio_artifact_id": timeline_envelope.artifact_id,
                "video_blob": _blob(proxy_hash, proxy_length, "video/mp4"),
                "width": width,
                "height": height,
                "max_width": media_config.proxy_max_width,
                "max_height": media_config.proxy_max_height,
                "target_video_bitrate_bps": media_config.proxy_video_bitrate_bps,
                "authoritative_for_audio_processing": False,
            }
            proxy_envelope = ArtifactEnvelope.create(
                artifact_kind="video_proxy",
                scope_key="global",
                producer=media_producer,
                inputs=[
                    source_input,
                    InputRef(role="media_probe", artifact_id=probe_envelope.artifact_id),
                    InputRef(role="timeline_audio", artifact_id=timeline_envelope.artifact_id),
                ],
                payload=proxy_payload,
            )

        silences = detect_silence_spans(timeline_temp, chunk_config)
        chunks = build_chunk_plan(probe.duration_ms, silences, chunk_config)
        chunk_plan_payload = {
            "duration_ms": probe.duration_ms,
            "timeline_audio_artifact_id": timeline_envelope.artifact_id,
            "config": asdict(chunk_config),
            "detected_silences": [
                {"global_start_ms": start, "global_end_ms": end} for start, end in silences
            ],
            "chunks": chunks,
        }
        chunk_plan_envelope = ArtifactEnvelope.create(
            artifact_kind="chunk_plan",
            scope_key="global",
            producer=chunk_producer,
            inputs=[InputRef(role="timeline_audio", artifact_id=timeline_envelope.artifact_id)],
            payload=chunk_plan_payload,
        )

        media_chunks: list[ArtifactEnvelope] = []
        for chunk in chunks:
            temp = _temp_path(context, ".wav")
            chunk_temps.append(temp)
            slice_wave(
                timeline_temp,
                temp,
                int(chunk["global_start_ms"]),
                int(chunk["global_end_ms"]),
            )
            chunk_hash, chunk_length, _ = context.store.publish_blob(temp)
            payload = {
                **chunk,
                "timeline_audio_artifact_id": timeline_envelope.artifact_id,
                "audio_blob": _blob(chunk_hash, chunk_length, "audio/wav"),
            }
            media_chunks.append(
                ArtifactEnvelope.create(
                    artifact_kind="media_chunk",
                    scope_key=str(chunk["chunk_id"]),
                    producer=chunk_producer,
                    inputs=[
                        InputRef(
                            role="timeline_audio",
                            artifact_id=timeline_envelope.artifact_id,
                            coordinate_range={
                                "global_start_ms": chunk["global_start_ms"],
                                "global_end_ms": chunk["global_end_ms"],
                            },
                        ),
                        InputRef(role="chunk_plan", artifact_id=chunk_plan_envelope.artifact_id),
                    ],
                    payload=payload,
                )
            )

        complete = [probe_envelope, timeline_envelope, chunk_plan_envelope, *media_chunks]
        if proxy_envelope is not None:
            complete.append(proxy_envelope)
        for envelope in complete:
            context.publisher.publish(envelope, make_current=False)
        context.registry.activate_artifacts(
            context.project_id,
            [item.artifact_id for item in complete],
            stale_targets=[
                ("media_chunk", None),
                ("video_proxy", "global"),
                *[
                    (kind, None)
                    for kind in (
                        "transcript",
                        "alignment",
                        "subtitle",
                        "qa",
                        "filler_review",
                        "srt_render",
                    )
                ],
            ],
        )
        return MediaBundle(
            probe=probe_envelope,
            timeline_audio=timeline_envelope,
            video_proxy=proxy_envelope,
            chunk_plan=chunk_plan_envelope,
            media_chunks=tuple(media_chunks),
        )
    finally:
        timeline_temp.unlink(missing_ok=True)
        if proxy_temp is not None:
            proxy_temp.unlink(missing_ok=True)
        for temp in chunk_temps:
            temp.unlink(missing_ok=True)


def render_timeline_audio(
    source: Path, probe: ProbeResult, destination: Path, runtime: RuntimeConfig
) -> None:
    config = MediaPrepConfig()
    audio_facts = probe.payload["audio_stream"]
    media_facts = probe.payload["video_stream"] or audio_facts
    offset_ms = int(audio_facts["start_time_ms"]) - int(media_facts["start_time_ms"])
    filters: list[str] = []
    if offset_ms < 0:
        filters.extend([f"atrim=start={-offset_ms / 1000:.3f}", "asetpts=PTS-STARTPTS"])
    elif offset_ms > 0:
        filters.extend(["asetpts=PTS-STARTPTS", f"adelay={offset_ms}:all=1"])
    else:
        filters.append("asetpts=PTS-STARTPTS")
    filters.extend(
        [
            f"aresample={config.sample_rate_hz}",
            "apad",
            f"atrim=duration={probe.duration_ms / 1000:.3f}",
        ]
    )
    command = [
        runtime.ffmpeg,
        "-v",
        "error",
        "-y",
        "-i",
        str(source),
        "-map",
        "0:a:0",
        "-af",
        ",".join(filters),
        "-ar",
        str(config.sample_rate_hz),
        "-ac",
        str(config.channels),
        "-c:a",
        "pcm_s16le",
        str(destination),
    ]
    _run(command)
    with wave.open(str(destination), "rb") as wav:
        actual_ms = round(wav.getnframes() * 1000 / wav.getframerate())
        if wav.getframerate() != 16_000 or wav.getnchannels() != 1 or wav.getsampwidth() != 2:
            raise ContractError("Timeline Audio is not 16kHz mono PCM s16le")
    if abs(actual_ms - probe.duration_ms) > config.timeline_tolerance_ms:
        raise ContractError("Timeline Audio duration does not match authoritative timeline")


def render_video_proxy(
    source: Path, timeline_audio: Path, duration_ms: int, destination: Path, runtime: RuntimeConfig
) -> None:
    config = MediaPrepConfig()
    scale = (
        f"setpts=PTS-STARTPTS,scale={config.proxy_max_width}:{config.proxy_max_height}:"
        "force_original_aspect_ratio=decrease:force_divisible_by=2"
    )
    command = [
        runtime.ffmpeg,
        "-v",
        "error",
        "-y",
        "-i",
        str(source),
        "-i",
        str(timeline_audio),
        "-map",
        "0:v:0",
        "-map",
        "1:a:0",
        "-vf",
        scale,
        "-c:v",
        "libx264",
        "-b:v",
        str(config.proxy_video_bitrate_bps),
        "-maxrate",
        str(config.proxy_video_bitrate_bps),
        "-bufsize",
        str(config.proxy_video_bitrate_bps * 2),
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-b:a",
        str(config.proxy_audio_bitrate_bps),
        "-t",
        f"{duration_ms / 1000:.3f}",
        "-movflags",
        "+faststart",
        str(destination),
    ]
    _run(command)


def probe_video_size(path: Path, runtime: RuntimeConfig) -> tuple[int, int]:
    raw = _run_json(
        [
            runtime.ffprobe,
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=width,height",
            "-of",
            "json",
            str(path),
        ]
    )
    streams = raw.get("streams")
    if not isinstance(streams, list) or not streams:
        raise ContractError("generated Video Proxy has no video stream")
    width = int(streams[0]["width"])
    height = int(streams[0]["height"])
    if width > 640 or height > 360:
        raise ContractError("generated Video Proxy exceeds frozen dimensions")
    return width, height


def detect_silence_spans(path: Path, config: ChunkerConfig) -> list[tuple[int, int]]:
    window_ms = 20
    threshold = 32767 * 10 ** (config.silence_threshold_db / 20)
    spans: list[tuple[int, int]] = []
    with wave.open(str(path), "rb") as wav:
        if wav.getnchannels() != 1 or wav.getsampwidth() != 2:
            raise ContractError("silence detection requires mono PCM s16le")
        rate = wav.getframerate()
        window_frames = max(1, rate * window_ms // 1000)
        silent_start: int | None = None
        frame_start = 0
        while data := wav.readframes(window_frames):
            samples = array("h")
            samples.frombytes(data)
            if sys.byteorder != "little":
                samples.byteswap()
            rms = math.sqrt(sum(sample * sample for sample in samples) / max(1, len(samples)))
            start_ms = round(frame_start * 1000 / rate)
            frame_start += len(samples)
            end_ms = round(frame_start * 1000 / rate)
            if rms <= threshold and silent_start is None:
                silent_start = start_ms
            elif rms > threshold and silent_start is not None:
                if start_ms - silent_start >= config.silence_min_duration_ms:
                    spans.append((silent_start, start_ms))
                silent_start = None
        if silent_start is not None:
            end_ms = round(frame_start * 1000 / rate)
            if end_ms - silent_start >= config.silence_min_duration_ms:
                spans.append((silent_start, end_ms))
    return spans


def build_chunk_plan(
    duration_ms: int,
    silence_spans: Sequence[tuple[int, int]],
    config: ChunkerConfig | None = None,
) -> list[dict[str, Any]]:
    chosen = config or ChunkerConfig()
    boundaries = [0]
    while duration_ms - boundaries[-1] > chosen.hard_limit_ms:
        start = boundaries[-1]
        target = start + chosen.target_duration_ms
        hard = min(duration_ms, start + chosen.hard_limit_ms)
        candidates = [
            (left + right) // 2
            for left, right in silence_spans
            if start < (left + right) // 2 <= hard
        ]
        boundary = (
            min(candidates, key=lambda item: (abs(item - target), item))
            if candidates
            else hard
        )
        if boundary <= start:
            raise ContractError("Chunker failed to make forward progress")
        boundaries.append(boundary)
    boundaries.append(duration_ms)
    return [
        {
            "chunk_id": f"chunk_{index + 1:04d}",
            "ordinal": index,
            "global_start_ms": start,
            "global_end_ms": end,
        }
        for index, (start, end) in enumerate(zip(boundaries, boundaries[1:], strict=False))
    ]


def slice_wave(source: Path, destination: Path, start_ms: int, end_ms: int) -> None:
    with wave.open(str(source), "rb") as input_wav:
        rate = input_wav.getframerate()
        start_frame = round(start_ms * rate / 1000)
        end_frame = round(end_ms * rate / 1000)
        input_wav.setpos(start_frame)
        frames = input_wav.readframes(end_frame - start_frame)
        with wave.open(str(destination), "wb") as output_wav:
            output_wav.setparams(input_wav.getparams())
            output_wav.setnframes(0)
            output_wav.writeframes(frames)


def _producer(component: str, config: Mapping[str, Any]) -> Producer:
    return Producer(
        component=component,
        component_version=COMPONENT_VERSION,
        processing_profile=None,
        provider=None,
        model=None,
        config_hash=hash_json(config),
    )


def _blob(content_hash: str, byte_length: int, media_type: str) -> dict[str, Any]:
    return {
        "content_hash": content_hash,
        "byte_length": byte_length,
        "media_type": media_type,
    }


def _temp_path(context: ProjectContext, suffix: str) -> Path:
    descriptor, raw_path = tempfile.mkstemp(
        prefix="media-", suffix=suffix, dir=context.store.temp_root
    )
    os.close(descriptor)
    return Path(raw_path)


def _run_json(command: Sequence[str]) -> dict[str, Any]:
    result = _run(command)
    try:
        value = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise ContractError("media tool returned invalid JSON") from exc
    if not isinstance(value, dict):
        raise ContractError("media tool JSON root must be an object")
    return value


def _run(command: Sequence[str]) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            list(command),
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except FileNotFoundError as exc:
        raise ProviderUnavailableError(f"media tool unavailable: {command[0]}") from exc
    except subprocess.CalledProcessError as exc:
        detail = exc.stderr.strip()[-2000:] if exc.stderr else "no diagnostic"
        raise ContractError(f"media command failed: {detail}") from exc


def _duration_ms(stream: Mapping[str, Any], format_info: Mapping[str, Any]) -> int:
    value = stream.get("duration") or format_info.get("duration")
    return _seconds_ms(value, default=0)


def _seconds_ms(value: Any, *, default: int) -> int:
    try:
        return round(float(value) * 1000)
    except (TypeError, ValueError):
        return default


def _stream_facts(stream: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "index": int(stream.get("index", -1)),
        "codec_name": str(stream.get("codec_name", "unknown")),
        "start_time_ms": _seconds_ms(stream.get("start_time"), default=0),
        "duration_ms": _seconds_ms(stream.get("duration"), default=0),
        "time_base": str(stream.get("time_base", "unknown")),
        "width": int(stream.get("width", 0)),
        "height": int(stream.get("height", 0)),
        "sample_rate_hz": int(stream.get("sample_rate", 0)),
        "channels": int(stream.get("channels", 0)),
    }


def _packet_discontinuity(packets: Any, audio_index: int, tolerance_ms: int) -> str | None:
    if not isinstance(packets, list):
        return "audio_packet_timeline_unavailable"
    previous_end: int | None = None
    for packet in packets:
        if not isinstance(packet, dict) or int(packet.get("stream_index", -2)) != audio_index:
            continue
        start = _seconds_ms(packet.get("pts_time"), default=-1)
        duration = _seconds_ms(packet.get("duration_time"), default=0)
        if start < 0:
            return "audio_packet_timestamp_unavailable"
        if previous_end is not None and (
            start < previous_end - tolerance_ms or start > previous_end + tolerance_ms
        ):
            return "audio_timestamp_discontinuity"
        previous_end = start + duration
    return None
