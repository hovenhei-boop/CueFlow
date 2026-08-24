from __future__ import annotations

import csv
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
from decimal import Decimal, InvalidOperation
from fractions import Fraction
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
    total_sample_count: int
    payload: dict[str, Any]


@dataclass(frozen=True)
class MediaBundle:
    probe: ArtifactEnvelope
    timeline_audio: ArtifactEnvelope
    chunk_plan: ArtifactEnvelope
    media_chunks: tuple[ArtifactEnvelope, ...]


def probe_source(path: Path, runtime: RuntimeConfig) -> ProbeResult:
    if not runtime.ffprobe:
        raise ProviderUnavailableError("ffprobe is required for Media Prep")
    metadata = _run_json(
        [
            runtime.ffprobe,
            "-v",
            "error",
            "-show_format",
            "-show_streams",
            "-of",
            "json",
            str(path),
        ]
    )
    config = MediaPrepConfig()
    opening = _run_json(
        [
            runtime.ffprobe,
            "-v",
            "error",
            "-read_intervals",
            f"%+{config.opening_scan_limit_ms / 1000:g}",
            "-show_frames",
            "-show_entries",
            (
                "frame=media_type,stream_index,pts,best_effort_timestamp,"
                "nb_samples,sample_rate:frame_side_data=side_data_type,skip_samples"
            ),
            "-of",
            "json",
            str(path),
        ]
    )
    streams = metadata.get("streams")
    if not isinstance(streams, list):
        raise ContractError("ffprobe returned no streams array")
    stream_items = [cast(dict[str, Any], item) for item in streams if isinstance(item, dict)]
    audio = next((item for item in stream_items if item.get("codec_type") == "audio"), None)
    if audio is None:
        raise ContractError("CueFlow v0.1.1 requires an audio stream")
    continuity = scan_packet_continuity(path, runtime, audio)
    return analyze_presentation_timeline(metadata, opening, continuity, config=config)


def analyze_presentation_timeline(
    metadata: Mapping[str, Any],
    opening: Mapping[str, Any],
    continuity: Mapping[str, Any],
    *,
    config: MediaPrepConfig | None = None,
) -> ProbeResult:
    chosen = config or MediaPrepConfig()
    streams_value = metadata.get("streams")
    if not isinstance(streams_value, list):
        raise ContractError("ffprobe returned no streams array")
    streams = [cast(dict[str, Any], item) for item in streams_value if isinstance(item, dict)]
    video = next((item for item in streams if item.get("codec_type") == "video"), None)
    audio = next((item for item in streams if item.get("codec_type") == "audio"), None)
    if audio is None:
        raise ContractError("CueFlow v0.1.1 requires an audio stream")
    media_kind = "video" if video is not None else "audio"
    format_value = metadata.get("format")
    format_info: Mapping[str, Any] = (
        cast(dict[str, Any], format_value) if isinstance(format_value, dict) else {}
    )
    authority = video if video is not None else audio
    duration = _duration_fraction(authority, format_info)
    if duration is None or duration <= 0:
        raise ContractError("media presentation duration is unavailable or non-positive")
    total_samples = quantize_samples(duration, chosen.sample_rate_hz)
    if total_samples <= 0:
        raise ContractError("media presentation duration is below one output sample")
    duration_ms = _sample_count_to_ms(total_samples, chosen.sample_rate_hz)

    frames_value = opening.get("frames")
    frames = [
        cast(dict[str, Any], item)
        for item in frames_value
        if isinstance(item, dict)
    ] if isinstance(frames_value, list) else []
    audio_evidence = _first_audio_sample(frames, audio)
    media_evidence = (
        _first_frame(frames, video) if video is not None else audio_evidence
    )
    issues: list[str] = []
    exact_offset: Fraction | None = None
    if audio_evidence is not None and media_evidence is not None:
        exact_offset = audio_evidence[0] - media_evidence[0]
    else:
        issues.append("opening_presentation_timestamp_unavailable")

    actions, offset_issue = build_timeline_actions(
        exact_offset,
        total_sample_count=total_samples,
        sample_rate_hz=chosen.sample_rate_hz,
    )
    if offset_issue is not None:
        issues.append(offset_issue)
    continuity_status = str(continuity.get("status", "unavailable"))
    if continuity_status != "continuous":
        anomaly = continuity.get("first_anomaly")
        code = (
            str(anomaly.get("code"))
            if isinstance(anomaly, Mapping) and anomaly.get("code")
            else "audio_packet_timeline_unavailable"
        )
        if code not in issues:
            issues.append(code)
    if exact_offset is None or continuity_status != "continuous":
        timeline_status = "unverified"
    elif quantize_samples(exact_offset, chosen.sample_rate_hz) == 0:
        timeline_status = "normal"
    else:
        timeline_status = "corrected"
    payload = {
        "media_kind": media_kind,
        "presentation_duration_ms": duration_ms,
        "presentation_total_samples": total_samples,
        "opening_scan_limit_ms": chosen.opening_scan_limit_ms,
        "timeline_status": timeline_status,
        "container": {
            "format_name": str(format_info.get("format_name", "unknown")),
            "start_time": _optional_string_value(format_info.get("start_time")),
            "duration": _optional_string_value(format_info.get("duration")),
        },
        "video_stream": _stream_facts(video) if video is not None else None,
        "audio_stream": _stream_facts(audio),
        "presentation_evidence": {
            "media_origin": media_evidence[1] if media_evidence is not None else None,
            "audio_start": audio_evidence[1] if audio_evidence is not None else None,
            "exact_offset": _fraction_payload(exact_offset) if exact_offset is not None else None,
        },
        "continuity_check": dict(continuity),
        "timeline_issues": issues,
        "timeline_actions": actions,
    }
    return ProbeResult(media_kind, duration_ms, total_samples, payload)


def build_timeline_actions(
    exact_offset: Fraction | None,
    *,
    total_sample_count: int,
    sample_rate_hz: int = 16_000,
) -> tuple[list[dict[str, Any]], str | None]:
    if total_sample_count <= 0:
        raise ContractError("presentation duration must contain at least one sample")
    issue: str | None = None
    if exact_offset is None:
        origin: dict[str, Any] = {"action": "timeline_origin_unverified"}
    else:
        samples = quantize_samples(exact_offset, sample_rate_hz)
        if samples > 0:
            origin = {"action": "pad_silence_before", "sample_count": samples}
            issue = "audio_starts_after_media"
        elif samples < 0:
            origin = {"action": "trim_before_timeline", "sample_count": -samples}
            issue = "audio_starts_before_media"
        else:
            origin = {"action": "timeline_origin_unchanged"}
    return [
        origin,
        {
            "action": "fit_presentation_duration",
            "total_sample_count": total_sample_count,
        },
    ], issue


def timeline_filters_from_actions(
    actions: Sequence[Mapping[str, Any]], config: MediaPrepConfig | None = None
) -> list[str]:
    chosen = config or MediaPrepConfig()
    filters = [f"aresample={chosen.sample_rate_hz}"]
    origin_count = 0
    duration_count = 0
    for action in actions:
        name = action.get("action")
        if name in {"timeline_origin_unchanged", "timeline_origin_unverified"}:
            origin_count += 1
            if "sample_count" in action:
                raise ContractError(f"{name} must not carry sample_count")
            filters.append("asetpts=PTS-STARTPTS")
        elif name == "pad_silence_before":
            origin_count += 1
            samples = _positive_action_integer(action, "sample_count")
            filters.extend(["asetpts=PTS-STARTPTS", f"adelay={samples}S:all=1"])
        elif name == "trim_before_timeline":
            origin_count += 1
            samples = _positive_action_integer(action, "sample_count")
            filters.extend([f"atrim=start_sample={samples}", "asetpts=PTS-STARTPTS"])
        elif name == "fit_presentation_duration":
            duration_count += 1
            samples = _positive_action_integer(action, "total_sample_count")
            filters.extend(["apad", f"atrim=end_sample={samples}"])
        else:
            raise ContractError(f"unknown timeline action: {name}")
    if origin_count != 1 or duration_count != 1:
        raise ContractError("timeline render requires one origin and one duration action")
    return filters


def scan_packet_continuity(
    path: Path, runtime: RuntimeConfig, audio_stream: Mapping[str, Any]
) -> dict[str, Any]:
    time_base = _time_base(audio_stream.get("time_base"))
    if time_base is None:
        return _continuity_unavailable("audio_packet_time_base_unavailable", 0)
    command = [
        runtime.ffprobe,
        "-v",
        "error",
        "-select_streams",
        "a:0",
        "-show_packets",
        "-show_entries",
        "packet=pts,duration",
        "-of",
        "csv=p=0",
        str(path),
    ]
    try:
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except FileNotFoundError as exc:
        raise ProviderUnavailableError(f"media tool unavailable: {runtime.ffprobe}") from exc
    assert process.stdout is not None
    previous_end: Fraction | None = None
    first_anomaly: dict[str, Any] | None = None
    scanned = 0
    for line in process.stdout:
        row = next(csv.reader([line]))
        if not row or not any(item.strip() for item in row):
            continue
        scanned += 1
        if len(row) < 2:
            first_anomaly = first_anomaly or {
                "code": "audio_packet_timestamp_unavailable",
                "packet_ordinal": scanned - 1,
            }
            previous_end = None
            continue
        pts = _optional_integer(row[0])
        duration = _optional_integer(row[1])
        if pts is None or duration is None or duration <= 0:
            first_anomaly = first_anomaly or {
                "code": "audio_packet_timestamp_unavailable",
                "packet_ordinal": scanned - 1,
            }
            previous_end = None
            continue
        start = pts * time_base
        if previous_end is not None and start != previous_end:
            first_anomaly = first_anomaly or {
                "code": (
                    "audio_timestamp_backward_jump"
                    if start < previous_end
                    else "audio_timestamp_gap"
                ),
                "packet_ordinal": scanned - 1,
                "expected": _fraction_payload(previous_end),
                "observed": _fraction_payload(start),
            }
        previous_end = start + duration * time_base
    stderr = process.stderr.read() if process.stderr is not None else ""
    return_code = process.wait()
    if return_code != 0:
        detail = stderr.strip()[-2000:] or "no diagnostic"
        raise ContractError(f"media command failed: {detail}")
    if scanned == 0:
        return _continuity_unavailable("audio_packet_timeline_unavailable", 0)
    return {
        "status": "discontinuous" if first_anomaly is not None else "continuous",
        "packets_scanned": scanned,
        "first_anomaly": first_anomaly,
    }


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
    chunk_temps: list[Path] = []
    try:
        render_timeline_audio(source_path, probe, timeline_temp, runtime)
        audio_hash, audio_length, _ = context.store.publish_blob(timeline_temp)
        timeline_payload = {
            "source_asset_id": str(source_asset["source_asset_id"]),
            "media_probe_artifact_id": probe_envelope.artifact_id,
            "duration_ms": probe.duration_ms,
            "total_sample_count": probe.total_sample_count,
            "sample_rate_hz": media_config.sample_rate_hz,
            "channels": media_config.channels,
            "sample_format": media_config.sample_format,
            "timeline_origin_sample": 0,
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
        for envelope in complete:
            context.publisher.publish(envelope, make_current=False)
        context.registry.activate_artifacts(
            context.project_id,
            [item.artifact_id for item in complete],
            stale_targets=[
                ("media_chunk", None),
                *[
                    (kind, None)
                    for kind in ("transcript", "alignment", "subtitle", "qa", "srt_render")
                ],
            ],
        )
        return MediaBundle(
            probe=probe_envelope,
            timeline_audio=timeline_envelope,
            chunk_plan=chunk_plan_envelope,
            media_chunks=tuple(media_chunks),
        )
    finally:
        timeline_temp.unlink(missing_ok=True)
        for temp in chunk_temps:
            temp.unlink(missing_ok=True)


def render_timeline_audio(
    source: Path, probe: ProbeResult, destination: Path, runtime: RuntimeConfig
) -> None:
    config = MediaPrepConfig()
    filters = timeline_filters_from_actions(probe.payload["timeline_actions"], config)
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
        if wav.getframerate() != 16_000 or wav.getnchannels() != 1 or wav.getsampwidth() != 2:
            raise ContractError("Timeline Audio is not 16kHz mono PCM s16le")
        if wav.getnframes() != probe.total_sample_count:
            raise ContractError("Timeline Audio sample length does not match presentation duration")


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


def quantize_samples(value: Fraction, sample_rate_hz: int) -> int:
    scaled_numerator = abs(value.numerator) * sample_rate_hz
    whole, remainder = divmod(scaled_numerator, value.denominator)
    if remainder * 2 >= value.denominator:
        whole += 1
    return whole if value >= 0 else -whole


def _first_frame(
    frames: Sequence[Mapping[str, Any]], stream: Mapping[str, Any]
) -> tuple[Fraction, dict[str, Any]] | None:
    stream_index = _optional_integer(stream.get("index"))
    time_base = _time_base(stream.get("time_base"))
    if stream_index is None or time_base is None:
        return None
    candidates: list[tuple[Fraction, dict[str, Any]]] = []
    for frame in frames:
        if _optional_integer(frame.get("stream_index")) != stream_index:
            continue
        pts = _frame_pts(frame)
        if pts is None:
            continue
        start = pts * time_base
        candidates.append((start, _frame_evidence(frame, stream, pts, start, 0)))
    return min(candidates, key=lambda item: item[0]) if candidates else None


def _first_audio_sample(
    frames: Sequence[Mapping[str, Any]], stream: Mapping[str, Any]
) -> tuple[Fraction, dict[str, Any]] | None:
    stream_index = _optional_integer(stream.get("index"))
    time_base = _time_base(stream.get("time_base"))
    sample_rate = _optional_integer(stream.get("sample_rate"))
    if stream_index is None or time_base is None or sample_rate is None or sample_rate <= 0:
        return None
    candidates: list[tuple[Fraction, dict[str, Any]]] = []
    for frame in frames:
        if _optional_integer(frame.get("stream_index")) != stream_index:
            continue
        pts = _frame_pts(frame)
        if pts is None:
            continue
        skip_samples = _skip_samples(frame)
        nb_samples = _optional_integer(frame.get("nb_samples"))
        if nb_samples is not None and skip_samples >= nb_samples:
            continue
        start = pts * time_base + Fraction(skip_samples, sample_rate)
        candidates.append(
            (start, _frame_evidence(frame, stream, pts, start, skip_samples))
        )
    return min(candidates, key=lambda item: item[0]) if candidates else None


def _frame_evidence(
    frame: Mapping[str, Any],
    stream: Mapping[str, Any],
    pts: int,
    valid_start: Fraction,
    skip_samples: int,
) -> dict[str, Any]:
    time_base = _time_base(stream.get("time_base"))
    assert time_base is not None
    return {
        "stream_index": int(stream["index"]),
        "pts": pts,
        "time_base_num": time_base.numerator,
        "time_base_den": time_base.denominator,
        "skip_samples": skip_samples,
        "nb_samples": _optional_integer(frame.get("nb_samples")),
        "sample_rate_hz": _optional_integer(frame.get("sample_rate"))
        or _optional_integer(stream.get("sample_rate")),
        "valid_start": _fraction_payload(valid_start),
    }


def _skip_samples(frame: Mapping[str, Any]) -> int:
    direct = _optional_integer(frame.get("skip_samples"))
    if direct is not None:
        return max(0, direct)
    side_data = frame.get("side_data_list")
    if not isinstance(side_data, list):
        return 0
    for item in side_data:
        if isinstance(item, Mapping):
            value = _optional_integer(item.get("skip_samples"))
            if value is not None:
                return max(0, value)
    return 0


def _frame_pts(frame: Mapping[str, Any]) -> int | None:
    best_effort = _optional_integer(frame.get("best_effort_timestamp"))
    return best_effort if best_effort is not None else _optional_integer(frame.get("pts"))


def _duration_fraction(
    stream: Mapping[str, Any], format_info: Mapping[str, Any]
) -> Fraction | None:
    duration_ts = _optional_integer(stream.get("duration_ts"))
    time_base = _time_base(stream.get("time_base"))
    if duration_ts is not None and duration_ts > 0 and time_base is not None:
        return duration_ts * time_base
    return _decimal_fraction(stream.get("duration")) or _decimal_fraction(
        format_info.get("duration")
    )


def _stream_facts(stream: Mapping[str, Any] | None) -> dict[str, Any]:
    if stream is None:
        raise ContractError("stream facts require a stream")
    time_base = _time_base(stream.get("time_base"))
    return {
        "index": _optional_integer(stream.get("index")),
        "codec_name": str(stream.get("codec_name", "unknown")),
        "start_pts": _optional_integer(stream.get("start_pts")),
        "duration_ts": _optional_integer(stream.get("duration_ts")),
        "time_base_num": time_base.numerator if time_base is not None else None,
        "time_base_den": time_base.denominator if time_base is not None else None,
        "sample_rate_hz": _optional_integer(stream.get("sample_rate")),
        "channels": _optional_integer(stream.get("channels")),
        "width": _optional_integer(stream.get("width")),
        "height": _optional_integer(stream.get("height")),
    }


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
    return {"content_hash": content_hash, "byte_length": byte_length, "media_type": media_type}


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


def _time_base(value: Any) -> Fraction | None:
    if not isinstance(value, str) or "/" not in value:
        return None
    numerator, denominator = value.split("/", 1)
    try:
        result = Fraction(int(numerator), int(denominator))
    except (ValueError, ZeroDivisionError):
        return None
    return result if result > 0 else None


def _decimal_fraction(value: Any) -> Fraction | None:
    if value is None:
        return None
    try:
        return Fraction(Decimal(str(value)))
    except (InvalidOperation, ValueError):
        return None


def _fraction_payload(value: Fraction) -> dict[str, int]:
    return {"numerator": value.numerator, "denominator": value.denominator}


def _optional_integer(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _optional_string_value(value: Any) -> str | None:
    return str(value) if value is not None else None


def _sample_count_to_ms(samples: int, sample_rate_hz: int) -> int:
    return (samples * 1000 + sample_rate_hz // 2) // sample_rate_hz


def _positive_action_integer(action: Mapping[str, Any], name: str) -> int:
    value = action.get(name)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ContractError(f"timeline action {name} must be a positive integer")
    return value


def _continuity_unavailable(code: str, scanned: int) -> dict[str, Any]:
    return {
        "status": "unavailable",
        "packets_scanned": scanned,
        "first_anomaly": {"code": code},
    }
