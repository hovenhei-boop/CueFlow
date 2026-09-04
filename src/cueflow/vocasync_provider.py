from __future__ import annotations

import os
import subprocess
import tempfile
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, cast

from cueflow.config import RuntimeConfig
from cueflow.errors import (
    ContractError,
    DeliveryAmbiguousError,
    ProviderError,
    ProviderUnavailableError,
)

_LEGACY_LANGUAGES = frozenset({"zh-CN", "en-US", "ja-JP", "ko-KR", "fr-FR", "es-ES"})


@dataclass(frozen=True)
class VocaSyncConfig:
    """Inactive v0.5.1 adapter settings retained only until the v0.5.3 cleanup."""

    api_base_url: str = "https://api.vocasync.com"
    request_timeout_seconds: float = 60.0
    poll_timeout_seconds: float = 900.0
    poll_interval_seconds: float = 2.0
    idempotency_header: str | None = None


def validate_alignment_language(value: Any) -> str:
    if not isinstance(value, str) or value not in _LEGACY_LANGUAGES:
        raise ContractError("language is unsupported by the inactive legacy VocaSync adapter")
    return value


@dataclass(frozen=True)
class AlignmentToken:
    text: str
    global_start_ms: int
    global_end_ms: int
    confidence: Mapping[str, Any] | None = None


@dataclass(frozen=True)
class RemoteJobState:
    project_uuid: str | None = None
    project_id: str | None = None
    job_id: str | None = None
    status: str | None = None
    artifacts: tuple[Mapping[str, Any], ...] = ()


@dataclass(frozen=True)
class AlignmentResult:
    tokens: tuple[AlignmentToken, ...]
    remote: RemoteJobState


class AlignmentProvider(Protocol):
    provider: str
    model: str | None
    revision: str | None

    def align(
        self,
        audio_path: Path,
        transcript_text: str,
        language: str,
        *,
        idempotency_key: str,
        on_remote_state: Callable[[RemoteJobState], None] | None = None,
    ) -> AlignmentResult: ...

    def close(self) -> None: ...


class VocaSyncAlignmentProvider:
    provider = "vocasync"
    model: str | None = None
    revision: str | None = None

    def __init__(
        self,
        *,
        config: VocaSyncConfig | None = None,
        client_factory: Callable[..., Any] | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.config = config or VocaSyncConfig()
        self._client_factory = client_factory
        self._sleep = sleep
        self._client: Any | None = None

    def align(
        self,
        audio_path: Path,
        transcript_text: str,
        language: str,
        *,
        idempotency_key: str,
        on_remote_state: Callable[[RemoteJobState], None] | None = None,
    ) -> AlignmentResult:
        validate_alignment_language(language)
        if not transcript_text.strip():
            raise ContractError("VocaSync Alignment requires a non-empty Transcript")
        if not audio_path.is_file():
            raise ContractError(f"VocaSync transport audio is missing: {audio_path}")
        client = self._get_client()
        callback = on_remote_state or (lambda _state: None)
        transcript_bytes = transcript_text.encode("utf-8")
        presign = self._json_request(
            lambda: client.post(
                "/v1/alignment/presign",
                json={
                    "audioFile": {
                        "name": audio_path.name,
                        "type": "audio/flac",
                        "size": audio_path.stat().st_size,
                    },
                    "transcriptFile": {
                        "name": "transcript.txt",
                        "type": "text/plain",
                        "size": len(transcript_bytes),
                    },
                    "language": language,
                },
            ),
            "VocaSync presign",
        )
        project_uuid, audio_target, transcript_target = _parse_presign(presign)
        callback(RemoteJobState(project_uuid=project_uuid, status="presigned"))
        self._upload(client, str(audio_target["uploadUrl"]), audio_path, "audio/flac")
        self._upload_bytes(
            client,
            str(transcript_target["uploadUrl"]),
            transcript_bytes,
            "text/plain; charset=utf-8",
        )
        callback(RemoteJobState(project_uuid=project_uuid, status="uploaded"))
        headers: dict[str, str] = {}
        if self.config.idempotency_header:
            headers[self.config.idempotency_header] = idempotency_key
        create = self._json_request(
            lambda: client.post(
                "/v1/alignment",
                headers=headers,
                json={
                    "projectUuid": project_uuid,
                    "audioFileKey": audio_target["key"],
                    "transcriptFileKey": transcript_target["key"],
                    "language": language,
                    "audioFileSizeBytes": audio_path.stat().st_size,
                    "transcriptFileSizeBytes": len(transcript_bytes),
                },
            ),
            "VocaSync alignment create",
            ambiguous_on_transport=True,
        )
        state = _parse_create(create, project_uuid)
        callback(state)
        completed = self._poll(client, state, callback)
        project = self._json_request(
            lambda: client.get(f"/v1/projects/{project_uuid}"),
            "VocaSync project lookup",
        )
        artifacts = _alignment_artifacts(project)
        artifact = artifacts[-1]
        artifact_id = str(artifact["id"])
        download = self._json_request(
            lambda: client.get(
                f"/v1/projects/{project_uuid}/artifacts/{artifact_id}/download",
                params={"fileType": "json"},
            ),
            "VocaSync artifact download lookup",
        )
        download_url = _required_string(download, "downloadUrl")
        response = self._request(
            lambda: client.get(download_url),
            "VocaSync alignment JSON download",
        )
        try:
            payload = response.json()
        except Exception as exc:
            raise ContractError("VocaSync alignment artifact must be JSON") from exc
        tokens = parse_vocasync_alignment_json(payload)
        final_state = RemoteJobState(
            project_uuid=completed.project_uuid,
            project_id=completed.project_id,
            job_id=completed.job_id,
            status="completed",
            artifacts=tuple(dict(item) for item in artifacts),
        )
        callback(final_state)
        return AlignmentResult(tuple(tokens), final_state)

    def close(self) -> None:
        if self._client is not None:
            close = getattr(self._client, "close", None)
            if callable(close):
                close()
            self._client = None

    def _get_client(self) -> Any:
        if self._client is not None:
            return self._client
        api_key = os.getenv("VOCASYNC_API_KEY")
        if not api_key:
            raise ProviderUnavailableError("VocaSync requires VOCASYNC_API_KEY")
        factory = self._client_factory or _httpx_factory()
        try:
            self._client = factory(
                base_url=self.config.api_base_url.rstrip("/"),
                headers={"Authorization": f"Bearer {api_key}"},
                timeout=self.config.request_timeout_seconds,
                follow_redirects=True,
            )
        except Exception as exc:
            raise ProviderUnavailableError("VocaSync HTTP client could not be created") from exc
        return self._client

    def _upload(self, client: Any, url: str, path: Path, media_type: str) -> None:
        try:
            stream = path.open("rb")
        except OSError as exc:
            raise ContractError(f"VocaSync upload source is unreadable: {path}") from exc
        with stream:
            response = self._request(
                lambda: client.put(
                    url,
                    content=stream,
                    headers={
                        "Content-Type": media_type,
                        "Content-Length": str(path.stat().st_size),
                    },
                ),
                "VocaSync audio upload",
            )
        self._check_response(response, "VocaSync audio upload")

    def _upload_bytes(self, client: Any, url: str, data: bytes, media_type: str) -> None:
        response = self._request(
            lambda: client.put(
                url,
                content=data,
                headers={
                    "Content-Type": media_type,
                    "Content-Length": str(len(data)),
                },
            ),
            "VocaSync transcript upload",
        )
        self._check_response(response, "VocaSync transcript upload")

    def _poll(
        self,
        client: Any,
        initial: RemoteJobState,
        callback: Callable[[RemoteJobState], None],
    ) -> RemoteJobState:
        if initial.job_id is None:
            raise ContractError("VocaSync create response omitted jobId")
        deadline = time.monotonic() + self.config.poll_timeout_seconds
        state = initial
        while state.status in {"pending", "processing"}:
            if time.monotonic() >= deadline:
                raise DeliveryAmbiguousError(
                    "VocaSync job did not reach a terminal state before poll timeout"
                )
            self._sleep(self.config.poll_interval_seconds)
            value = self._json_request(
                lambda: client.get(f"/v1/jobs/{initial.job_id}"),
                "VocaSync job poll",
            )
            status = _required_string(value, "status")
            if status not in {"pending", "processing", "completed", "failed", "canceled"}:
                raise ContractError("VocaSync returned an unknown job status")
            state = RemoteJobState(
                project_uuid=initial.project_uuid,
                project_id=initial.project_id,
                job_id=initial.job_id,
                status=status,
            )
            callback(state)
        if state.status != "completed":
            raise ProviderError(f"VocaSync alignment finished with status={state.status}")
        return state

    def _json_request(
        self,
        operation: Callable[[], Any],
        name: str,
        *,
        ambiguous_on_transport: bool = False,
    ) -> Mapping[str, Any]:
        response = self._request(operation, name, ambiguous_on_transport=ambiguous_on_transport)
        self._check_response(response, name)
        try:
            value = response.json()
        except Exception as exc:
            raise ContractError(f"{name} returned invalid JSON") from exc
        if not isinstance(value, Mapping):
            raise ContractError(f"{name} JSON root must be an object")
        return cast(Mapping[str, Any], value)

    @staticmethod
    def _request(
        operation: Callable[[], Any],
        name: str,
        *,
        ambiguous_on_transport: bool = False,
    ) -> Any:
        try:
            return operation()
        except Exception as exc:
            if ambiguous_on_transport:
                raise DeliveryAmbiguousError(
                    f"{name} may have been delivered; automatic retry is forbidden"
                ) from exc
            raise ProviderUnavailableError(f"{name} transport failed") from exc

    @staticmethod
    def _check_response(response: Any, name: str) -> None:
        status = getattr(response, "status_code", None)
        if not isinstance(status, int):
            raise ContractError(f"{name} returned no HTTP status")
        if 200 <= status < 300:
            return
        body = getattr(response, "text", "")
        detail = str(body)[-1000:] if body else "no response body"
        raise ProviderError(f"{name} failed with HTTP {status}: {detail}")


def build_alignment_transport(
    timeline_wav: Path, destination: Path, runtime: RuntimeConfig
) -> Path:
    if not runtime.ffmpeg:
        raise ProviderUnavailableError(
            "VocaSync transport conversion requires CUEFLOW_FFMPEG or ffmpeg on PATH"
        )
    command = [
        runtime.ffmpeg,
        "-v",
        "error",
        "-y",
        "-i",
        str(timeline_wav),
        "-map",
        "0:a:0",
        "-ar",
        "16000",
        "-ac",
        "1",
        "-c:a",
        "flac",
        str(destination),
    ]
    try:
        subprocess.run(command, check=True, capture_output=True, text=True)
    except FileNotFoundError as exc:
        raise ProviderUnavailableError(f"ffmpeg is unavailable: {runtime.ffmpeg}") from exc
    except subprocess.CalledProcessError as exc:
        detail = exc.stderr.strip()[-2000:] if exc.stderr else "no diagnostic"
        raise ContractError(f"lossless FLAC conversion failed: {detail}") from exc
    if not destination.is_file() or destination.stat().st_size <= 0:
        raise ContractError("lossless FLAC conversion produced no audio")
    return destination


def temporary_flac_path(directory: Path) -> Path:
    descriptor, raw = tempfile.mkstemp(prefix="vocasync-", suffix=".flac", dir=directory)
    os.close(descriptor)
    return Path(raw)


def parse_vocasync_alignment_json(value: Any) -> list[AlignmentToken]:
    if not isinstance(value, Mapping) or not isinstance(value.get("words"), list):
        raise ContractError(
            "VocaSync alignment JSON does not match the expected words[] contract; "
            "a live artifact-schema probe is required"
        )
    tokens: list[AlignmentToken] = []
    previous_end = -1
    for raw in value["words"]:
        if not isinstance(raw, Mapping):
            raise ContractError("VocaSync words[] entries must be objects")
        text = raw.get("text")
        start = raw.get("start")
        end = raw.get("end")
        if not isinstance(text, str) or not text:
            raise ContractError("VocaSync word text must be non-empty")
        if isinstance(start, bool) or not isinstance(start, (int, float)):
            raise ContractError("VocaSync word start must be seconds")
        if isinstance(end, bool) or not isinstance(end, (int, float)):
            raise ContractError("VocaSync word end must be seconds")
        start_ms = round(float(start) * 1000)
        end_ms = round(float(end) * 1000)
        if start_ms < previous_end or end_ms <= start_ms:
            raise ContractError("VocaSync word timings overlap or are invalid")
        confidence = raw.get("confidence")
        metadata = (
            {"value": confidence}
            if isinstance(confidence, (int, float)) and not isinstance(confidence, bool)
            else None
        )
        tokens.append(AlignmentToken(text, start_ms, end_ms, metadata))
        previous_end = end_ms
    if not tokens:
        raise ContractError("VocaSync alignment JSON contains no words")
    return tokens


def _parse_presign(
    value: Mapping[str, Any],
) -> tuple[str, Mapping[str, Any], Mapping[str, Any]]:
    project_uuid = _required_string(value, "projectUuid")
    audio = value.get("audio")
    transcript = value.get("transcript")
    if not isinstance(audio, Mapping) or not isinstance(transcript, Mapping):
        raise ContractError("VocaSync presign response omitted upload targets")
    for name, target in (("audio", audio), ("transcript", transcript)):
        _required_string(target, "key")
        upload_url = _required_string(target, "uploadUrl")
        if not upload_url.startswith("https://"):
            raise ContractError(f"VocaSync {name} upload URL must use HTTPS")
    return project_uuid, audio, transcript


def _parse_create(value: Mapping[str, Any], expected_uuid: str) -> RemoteJobState:
    project_uuid = _required_string(value, "projectUuid")
    if project_uuid != expected_uuid:
        raise ContractError("VocaSync create response changed projectUuid")
    status = _required_string(value, "status")
    if status != "pending":
        raise ContractError("VocaSync create response must begin at pending")
    return RemoteJobState(
        project_uuid=project_uuid,
        project_id=_required_string(value, "projectId"),
        job_id=_required_string(value, "jobId"),
        status=status,
    )


def _alignment_artifacts(project: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    artifacts = project.get("artifacts")
    if not isinstance(artifacts, list):
        raise ContractError("VocaSync project response omitted artifacts")
    result = [
        item
        for item in artifacts
        if isinstance(item, Mapping) and item.get("artifactType") == "alignment"
    ]
    if not result:
        raise ContractError("VocaSync completed project has no alignment artifact")
    for item in result:
        _required_string(item, "id")
    return result


def _required_string(value: Mapping[str, Any], name: str) -> str:
    result = value.get(name)
    if not isinstance(result, str) or not result:
        raise ContractError(f"VocaSync response field {name} must be a string")
    return result


def _httpx_factory() -> Callable[..., Any]:
    try:
        import httpx
    except ImportError as exc:
        raise ProviderUnavailableError("VocaSync requires the httpx dependency") from exc
    return cast(Callable[..., Any], httpx.Client)
