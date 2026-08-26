from __future__ import annotations

import json
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, cast

from cueflow.canonical import hash_json
from cueflow.config import (
    CLOUD_DOCUMENT_MODEL,
    CLOUD_REFERENCE_ASR_MODEL,
    COMPONENT_VERSION,
    REFERENCE_MODEL_SENT_ATTEMPT_LIMIT,
    REFERENCE_VISION_MODEL,
    RuntimeConfig,
)
from cueflow.errors import (
    ContractError,
    DeliveryAmbiguousError,
    ReferenceRunFailedError,
    UnsupportedReferenceError,
)
from cueflow.project import ProjectContext
from cueflow.reference_assets import inspect_reference, resolve_reference_locator
from cueflow.reference_documents import (
    classify_pdf,
    extract_ooxml,
    extract_text_cues,
    extract_text_document,
    extract_text_layer_pdf,
)
from cueflow.reference_media import (
    BitmapCueSet,
    FrameWindow,
    ReferenceMediaProbe,
    ReferenceWorkSpec,
    extract_audio_segment,
    extract_bitmap_cues,
    extract_frame_window,
    extract_text_subtitle_track,
    plan_reference_media_work,
    prepare_visual_image,
    probe_reference_media,
)
from cueflow.reference_providers import (
    CloudDocumentProvider,
    CloudDocumentRequest,
    CloudReferenceAsr,
    CloudReferenceVision,
    QwenCloudDocumentParser,
    ReferenceAsrProvider,
    ReferenceAsrRequest,
    ReferenceModelResult,
    ReferenceVisionProvider,
    ReferenceVisionRequest,
    cloud_asr_actual_config,
    cloud_document_actual_config,
    reference_vision_actual_config,
)
from cueflow.schema import ArtifactEnvelope, InputRef, Producer


@dataclass
class ReferenceProviders:
    asr: ReferenceAsrProvider | None = None
    vision: ReferenceVisionProvider | None = None
    document: CloudDocumentProvider | None = None


@dataclass(frozen=True)
class _InvocationResult:
    value: ReferenceModelResult
    invocation_id: str

    @property
    def text(self) -> str:
        return self.value.text

    @property
    def segments(self) -> tuple[dict[str, Any], ...]:
        return self.value.segments


class _ProviderPool:
    def __init__(
        self,
        supplied: ReferenceProviders | None,
    ) -> None:
        self.providers = supplied or ReferenceProviders()
        self._owned: list[Any] = []

    def asr(self) -> ReferenceAsrProvider:
        if self.providers.asr is None:
            provider = CloudReferenceAsr()
            self.providers.asr = provider
            self._owned.append(provider)
        return self.providers.asr

    def vision(self) -> ReferenceVisionProvider:
        if self.providers.vision is None:
            provider = CloudReferenceVision()
            self.providers.vision = provider
            self._owned.append(provider)
        return self.providers.vision

    def document(self) -> CloudDocumentProvider:
        if self.providers.document is None:
            provider = QwenCloudDocumentParser()
            self.providers.document = provider
            self._owned.append(provider)
        return self.providers.document

    def close(self) -> None:
        for provider in reversed(self._owned):
            provider.close()


def extract_reference(
    context: ProjectContext,
    reference_asset_id: str,
    *,
    pixel_subtitle_mode: Literal["burned", "none"] | None = None,
    runtime: RuntimeConfig | None = None,
    providers: ReferenceProviders | None = None,
) -> dict[str, Any]:
    context.registry.recover_running_reference_runs()
    chosen_runtime = runtime or RuntimeConfig.detect()
    reference = context.registry.reference_asset(reference_asset_id)
    path = resolve_reference_locator(context, reference_asset_id)
    inspection = inspect_reference(path, runtime=chosen_runtime)
    if (
        inspection.detected_format != reference["detected_format"]
        or inspection.media_category != reference["media_category"]
    ):
        raise UnsupportedReferenceError(
            "Reference current file format/category differs from its filename-bound registration"
        )
    specs = _plan_work(
        path,
        detected_format=inspection.detected_format,
        media_category=inspection.media_category,
        pixel_subtitle_mode=pixel_subtitle_mode,
        runtime=chosen_runtime,
    )
    run_config = _reference_run_config(pixel_subtitle_mode)
    run_id = context.registry.create_reference_run(
        context.project_id,
        reference_asset_id,
        {
            "reference_asset_id": reference_asset_id,
            "filename": str(reference["filename"]),
            "locator": str(path),
            "detected_format": inspection.detected_format,
            "media_category": inspection.media_category,
        },
        hash_json(run_config),
    )
    for ordinal, spec in enumerate(specs):
        context.registry.create_reference_work_item(
            run_id=run_id,
            ordinal=ordinal,
            branch=spec.branch,
            evidence_role=spec.evidence_role,
            work_spec=spec.as_dict(),
        )
    context.registry.set_run_status(run_id, "running")
    pool = _ProviderPool(providers)
    try:
        for row in context.registry.reference_work_items_for_run(run_id):
            _execute_work_item(
                context,
                dict(reference),
                dict(row),
                path=path,
                detected_format=inspection.detected_format,
                runtime=chosen_runtime,
                pool=pool,
                retry_reason=None,
            )
        result = _finalize_reference_run(context, run_id, publish_bundle=True)
    except KeyboardInterrupt:
        _interrupt_reference_run(context, run_id)
        raise
    except BaseException as exc:
        context.registry.finalize_interrupted_run(
            run_id,
            run_status="failed",
            error_message=str(exc) or type(exc).__name__,
        )
        raise
    finally:
        pool.close()
    if result["outcome"] != "complete":
        raise ReferenceRunFailedError(run_id, str(result["outcome"]))
    return result


def retry_reference_work_item(
    context: ProjectContext,
    work_item_id: str,
    *,
    runtime: RuntimeConfig | None = None,
    providers: ReferenceProviders | None = None,
) -> dict[str, Any]:
    context.registry.recover_running_reference_runs()
    item = context.registry.reference_work_item(work_item_id)
    if item["status"] not in {"failed", "interrupted"}:
        raise ContractError("Reference retry requires a failed or interrupted work item")
    run_id = str(item["run_id"])
    run = context.registry.reference_run(run_id)
    reference = context.registry.reference_asset(str(run["reference_asset_id"]))
    path = resolve_reference_locator(context, str(run["reference_asset_id"]))
    chosen_runtime = runtime or RuntimeConfig.detect()
    inspection = inspect_reference(path, runtime=chosen_runtime)
    if (
        inspection.detected_format != reference["detected_format"]
        or inspection.media_category != reference["media_category"]
    ):
        raise UnsupportedReferenceError(
            "Reference current file format/category differs from its filename-bound registration"
        )
    context.registry.reopen_run_for_retry(run_id)
    pool = _ProviderPool(providers)
    succeeded = False
    try:
        succeeded = _execute_work_item(
            context,
            dict(reference),
            dict(context.registry.reference_work_item(work_item_id)),
            path=path,
            detected_format=inspection.detected_format,
            runtime=chosen_runtime,
            pool=pool,
            retry_reason="explicit_work_item_retry",
        )
        result = _finalize_reference_run(context, run_id, publish_bundle=succeeded)
    except KeyboardInterrupt:
        _interrupt_reference_run(context, run_id)
        raise
    except BaseException as exc:
        context.registry.finalize_interrupted_run(
            run_id,
            run_status="failed",
            error_message=str(exc) or type(exc).__name__,
        )
        raise
    finally:
        pool.close()
    if result["outcome"] != "complete":
        raise ReferenceRunFailedError(run_id, str(result["outcome"]))
    return result


def reference_status(
    context: ProjectContext,
    reference_asset_id: str | None = None,
) -> dict[str, Any]:
    if reference_asset_id is not None:
        assets = [context.registry.reference_asset(reference_asset_id)]
    else:
        assets = context.registry.reference_assets()
    result_assets: list[dict[str, Any]] = []
    for asset in assets:
        asset_id = str(asset["reference_asset_id"])
        runs: list[dict[str, Any]] = []
        for run in context.registry.reference_runs(asset_id):
            run_id = str(run["run_id"])
            runs.append(
                {
                    **dict(run),
                    "work_items": [
                        _work_item_status(context, dict(row))
                        for row in context.registry.reference_work_items_for_run(run_id)
                    ],
                }
            )
        latest = runs[-1] if runs else None
        result_assets.append(
            {
                **dict(asset),
                "latest_run": (
                    {
                        "run_id": latest["run_id"],
                        "status": latest["status"],
                        "outcome": latest["outcome"],
                        "error_message": latest["error_message"],
                    }
                    if latest is not None
                    else None
                ),
                "runs": runs,
            }
        )
    return {"reference_assets": result_assets}


def _plan_work(
    path: Path,
    *,
    detected_format: str,
    media_category: str,
    pixel_subtitle_mode: Literal["burned", "none"] | None,
    runtime: RuntimeConfig,
) -> tuple[ReferenceWorkSpec, ...]:
    if media_category != "video" and pixel_subtitle_mode is not None:
        raise ContractError("pixel_subtitle_mode is only valid for video References")
    if media_category in {"audio", "video"}:
        probe = probe_reference_media(path, runtime)
        specs = plan_reference_media_work(probe, pixel_subtitle_mode)
        return tuple(_with_media_facts(spec, probe) for spec in specs)
    if media_category == "image":
        return (
            ReferenceWorkSpec(
                "image_visual",
                "image_visual",
                "image_vision",
                {},
            ),
        )
    if detected_format in {"txt", "md"}:
        kind = "text_document"
        role = "document_text"
    elif detected_format in {"srt", "vtt", "ass"}:
        kind = "cue_document"
        role = "text_subtitle"
    elif detected_format in {"docx", "pptx", "xlsx"}:
        kind = "ooxml"
        role = "document_text"
    elif detected_format in {"doc", "ppt", "xls"}:
        kind = "cloud_document"
        role = "cloud_document_parse"
    elif detected_format == "pdf":
        classification = classify_pdf(path)
        if classification.route == "document_text":
            kind = "pdf_text"
            role = "document_text"
        else:
            kind = "cloud_document"
            role = "cloud_document_parse"
    else:
        raise UnsupportedReferenceError(f"unsupported Reference document format: {detected_format}")
    return (
        ReferenceWorkSpec(
            branch=role,
            evidence_role=role,
            kind=kind,
            config={},
        ),
    )


def _with_media_facts(
    spec: ReferenceWorkSpec, probe: ReferenceMediaProbe
) -> ReferenceWorkSpec:
    return ReferenceWorkSpec(
        branch=spec.branch,
        evidence_role=spec.evidence_role,
        kind=spec.kind,
        config={
            **spec.config,
            "local_measured_duration_ms": probe.local_measured_duration_ms,
            "detected_format": probe.detected_format,
        },
    )


def _execute_work_item(
    context: ProjectContext,
    reference: Mapping[str, Any],
    item: Mapping[str, Any],
    *,
    path: Path,
    detected_format: str,
    runtime: RuntimeConfig,
    pool: _ProviderPool,
    retry_reason: str | None,
) -> bool:
    work_item_id = str(item["work_item_id"])
    spec = _json_mapping(str(item["work_spec_json"]), "Reference work spec")
    kind = str(spec["kind"])
    config = cast(Mapping[str, Any], spec["config"])
    context.registry.set_reference_work_item_status(work_item_id, "running")
    try:
        with tempfile.TemporaryDirectory(
            prefix=f"reference-{work_item_id}-",
            dir=context.store.temp_root,
        ) as raw_temp:
            temp_root = Path(raw_temp)
            if kind in {
                "text_document",
                "cue_document",
                "ooxml",
                "pdf_text",
                "text_subtitle",
            }:
                evidence = _execute_deterministic(
                    context,
                    reference,
                    item,
                    kind=kind,
                    config=config,
                    path=path,
                    detected_format=detected_format,
                    runtime=runtime,
                    temp_root=temp_root,
                )
            elif kind == "asr_unavailable":
                raise UnsupportedReferenceError(str(config["reason"]))
            elif kind == "asr":
                evidence = _execute_asr(
                    context,
                    reference,
                    item,
                    config=config,
                    path=path,
                    detected_format=detected_format,
                    runtime=runtime,
                    temp_root=temp_root,
                    pool=pool,
                    retry_reason=retry_reason,
                )
            elif kind == "frame_vision":
                evidence = _execute_frame_vision(
                    context,
                    reference,
                    item,
                    config=config,
                    path=path,
                    detected_format=detected_format,
                    runtime=runtime,
                    temp_root=temp_root,
                    pool=pool,
                    retry_reason=retry_reason,
                )
            elif kind == "bitmap_vision":
                evidence = _execute_bitmap_vision(
                    context,
                    reference,
                    item,
                    config=config,
                    path=path,
                    detected_format=detected_format,
                    runtime=runtime,
                    temp_root=temp_root,
                    pool=pool,
                    retry_reason=retry_reason,
                )
            elif kind == "image_vision":
                evidence = _execute_image_vision(
                    context,
                    reference,
                    item,
                    path=path,
                    detected_format=detected_format,
                    runtime=runtime,
                    temp_root=temp_root,
                    pool=pool,
                    retry_reason=retry_reason,
                )
            elif kind == "cloud_document":
                evidence = _execute_cloud_document(
                    context,
                    reference,
                    item,
                    path=path,
                    detected_format=detected_format,
                    pool=pool,
                    retry_reason=retry_reason,
                )
            else:
                raise ContractError(f"unknown Reference work item kind: {kind}")
        context.registry.set_reference_work_item_status(
            work_item_id,
            "succeeded",
            evidence_artifact_id=evidence.artifact_id,
        )
        return True
    except KeyboardInterrupt:
        context.registry.set_reference_work_item_status(
            work_item_id,
            "interrupted",
            failure_code="keyboard_interrupt",
            failure_details={"message": "Reference work item interrupted by user"},
        )
        raise
    except Exception as exc:
        context.registry.set_reference_work_item_status(
            work_item_id,
            "failed",
            failure_code=type(exc).__name__,
            failure_details={"message": str(exc)},
        )
        return False


def _execute_deterministic(
    context: ProjectContext,
    reference: Mapping[str, Any],
    item: Mapping[str, Any],
    *,
    kind: str,
    config: Mapping[str, Any],
    path: Path,
    detected_format: str,
    runtime: RuntimeConfig,
    temp_root: Path,
) -> ArtifactEnvelope:
    if kind == "text_document":
        extraction = extract_text_document(path, detected_format)
    elif kind == "cue_document":
        extraction = extract_text_cues(path, detected_format)
    elif kind == "ooxml":
        extraction = extract_ooxml(path, detected_format)
    elif kind == "pdf_text":
        extraction = extract_text_layer_pdf(path)
    elif kind == "text_subtitle":
        extraction = extract_text_subtitle_track(
            path,
            _int_config(config, "stream_index"),
            runtime,
            temp_root,
        )
    else:
        raise ContractError(f"unknown deterministic Reference kind: {kind}")
    duration = _local_duration_seconds(config)
    input_artifact = _publish_reference_input(
        context,
        reference,
        item,
        detected_format=detected_format,
        input_kind=kind,
        local_duration=duration,
        manifest={
            "filename": str(reference["filename"]),
            "locator_at_run": str(path),
            "parser": extraction.metadata.get("parser", "deterministic"),
            "config": dict(config),
        },
    )
    return _publish_reference_evidence(
        context,
        reference,
        item,
        input_artifact=input_artifact,
        content=extraction.content(),
        provenance={
            "detected_format": detected_format,
            "extractor": extraction.metadata.get("parser", "deterministic"),
            "config": dict(config),
        },
        local_duration=duration,
        result=None,
        provider=None,
        model=None,
    )


def _execute_asr(
    context: ProjectContext,
    reference: Mapping[str, Any],
    item: Mapping[str, Any],
    *,
    config: Mapping[str, Any],
    path: Path,
    detected_format: str,
    runtime: RuntimeConfig,
    temp_root: Path,
    pool: _ProviderPool,
    retry_reason: str | None,
) -> ArtifactEnvelope:
    start_ms = _int_config(config, "start_ms")
    end_ms = _int_config(config, "end_ms")
    local_duration = (end_ms - start_ms) / 1000
    previous = _previous_reference_input(context, str(item["work_item_id"]))
    if previous is None:
        wav = extract_audio_segment(
            path,
            start_ms,
            end_ms,
            runtime,
            temp_root / "segment.wav",
        )
        content_hash, byte_length, _ = context.store.publish_blob(wav)
        input_artifact = _publish_reference_input(
            context,
            reference,
            item,
            detected_format=detected_format,
            input_kind="pcm_wav_segment",
            local_duration=local_duration,
            manifest={
                "start_ms": start_ms,
                "end_ms": end_ms,
                "blob": {
                    "content_hash": content_hash,
                    "byte_length": byte_length,
                    "media_type": "audio/wav; codecs=pcm_s16le",
                },
            },
        )
    else:
        input_artifact = previous
    blob = cast(Mapping[str, Any], input_artifact.payload["manifest"])["blob"]
    blob_map = cast(Mapping[str, Any], blob)
    audio_path = context.store.blob_path(str(blob_map["content_hash"]))
    provider = pool.asr()
    actual_config = cloud_asr_actual_config()
    result = _invoke_model(
        context,
        item,
        provider=provider,
        operation="reference_asr",
        actual_config=actual_config,
        input_artifacts=(("reference_input", input_artifact.artifact_id),),
        local_duration=local_duration,
        retry_reason=retry_reason,
        call=lambda: provider.transcribe(
            ReferenceAsrRequest(audio_path, start_ms, end_ms)
        ),
    )
    return _publish_model_evidence(
        context,
        reference,
        item,
        input_artifact=input_artifact,
        result=result,
        provider=provider.provider,
        model=provider.model,
        local_duration=local_duration,
        content={"text": result.text, "segments": list(result.segments)},
        provenance={"start_ms": start_ms, "end_ms": end_ms, "config": actual_config},
    )


def _execute_frame_vision(
    context: ProjectContext,
    reference: Mapping[str, Any],
    item: Mapping[str, Any],
    *,
    config: Mapping[str, Any],
    path: Path,
    detected_format: str,
    runtime: RuntimeConfig,
    temp_root: Path,
    pool: _ProviderPool,
    retry_reason: str | None,
) -> ArtifactEnvelope:
    start_ms = _int_config(config, "start_ms")
    end_ms = _int_config(config, "end_ms")
    window = extract_frame_window(path, start_ms, end_ms, runtime, temp_root / "frames")
    manifest = _frame_manifest(window)
    previous = _previous_reference_input(context, str(item["work_item_id"]))
    if previous is None:
        input_artifact = _publish_reference_input(
            context,
            reference,
            item,
            detected_format=detected_format,
            input_kind="frame_manifest",
            local_duration=(end_ms - start_ms) / 1000,
            manifest=manifest,
        )
    else:
        if previous.payload["manifest"] != manifest:
            raise ContractError(
                "Reference Vision retry frames differ from the original input manifest"
            )
        input_artifact = previous
    provider = pool.vision()
    actual_config = reference_vision_actual_config(str(item["evidence_role"]))
    result = _invoke_model(
        context,
        item,
        provider=provider,
        operation="reference_vision",
        actual_config=actual_config,
        input_artifacts=(("reference_input", input_artifact.artifact_id),),
        local_duration=(end_ms - start_ms) / 1000,
        retry_reason=retry_reason,
        call=lambda: provider.recognize(
            ReferenceVisionRequest(
                tuple(frame.path for frame in window.frames),
                str(item["evidence_role"]),
                manifest,
            )
        ),
    )
    return _publish_model_evidence(
        context,
        reference,
        item,
        input_artifact=input_artifact,
        result=result,
        provider=provider.provider,
        model=provider.model,
        local_duration=(end_ms - start_ms) / 1000,
        content={"text": result.text},
        provenance={
            "frame_manifest": manifest,
            "model_frame_index_is_authoritative": False,
            "config": actual_config,
        },
    )


def _execute_bitmap_vision(
    context: ProjectContext,
    reference: Mapping[str, Any],
    item: Mapping[str, Any],
    *,
    config: Mapping[str, Any],
    path: Path,
    detected_format: str,
    runtime: RuntimeConfig,
    temp_root: Path,
    pool: _ProviderPool,
    retry_reason: str | None,
) -> ArtifactEnvelope:
    previous = _previous_reference_input(context, str(item["work_item_id"]))
    if previous is None:
        media_probe = probe_reference_media(path, runtime)
        cues = extract_bitmap_cues(
            path,
            _int_config(config, "stream_index"),
            str(config["codec"]),
            media_probe,
            runtime,
            temp_root / "bitmaps",
        )
        manifest = _persist_bitmap_manifest(context, cues)
        input_artifact = _publish_reference_input(
            context,
            reference,
            item,
            detected_format=detected_format,
            input_kind="unique_bitmap_cues",
            local_duration=_local_duration_seconds(config),
            manifest=manifest,
        )
    else:
        input_artifact = previous
        manifest = cast(dict[str, Any], dict(input_artifact.payload["manifest"]))
    images = tuple(
        context.store.blob_path(str(bitmap["blob"]["content_hash"]))
        for bitmap in cast(list[dict[str, Any]], manifest["unique_bitmaps"])
    )
    provider = pool.vision()
    actual_config = reference_vision_actual_config(str(item["evidence_role"]))
    result = _invoke_model(
        context,
        item,
        provider=provider,
        operation="reference_vision",
        actual_config=actual_config,
        input_artifacts=(("reference_input", input_artifact.artifact_id),),
        local_duration=_local_duration_seconds(config),
        retry_reason=retry_reason,
        call=lambda: provider.recognize(
            ReferenceVisionRequest(images, str(item["evidence_role"]), manifest)
        ),
    )
    return _publish_model_evidence(
        context,
        reference,
        item,
        input_artifact=input_artifact,
        result=result,
        provider=provider.provider,
        model=provider.model,
        local_duration=_local_duration_seconds(config),
        content={"text": result.text},
        provenance={"bitmap_manifest": manifest, "config": actual_config},
    )


def _execute_image_vision(
    context: ProjectContext,
    reference: Mapping[str, Any],
    item: Mapping[str, Any],
    *,
    path: Path,
    detected_format: str,
    runtime: RuntimeConfig,
    temp_root: Path,
    pool: _ProviderPool,
    retry_reason: str | None,
) -> ArtifactEnvelope:
    image, command, encoded_hash = prepare_visual_image(
        path, runtime, temp_root / "reference-image.jpg"
    )
    manifest = {
        "encoded_sha256": encoded_hash,
        "command_profile": _stable_ffmpeg_command(command),
        "source_kind": "filename-bound Reference image",
    }
    previous = _previous_reference_input(context, str(item["work_item_id"]))
    if previous is None:
        input_artifact = _publish_reference_input(
            context,
            reference,
            item,
            detected_format=detected_format,
            input_kind="image_manifest",
            local_duration=None,
            manifest=manifest,
        )
    else:
        if previous.payload["manifest"] != manifest:
            raise ContractError("Reference image retry differs from the original input manifest")
        input_artifact = previous
    provider = pool.vision()
    actual_config = reference_vision_actual_config("image_visual")
    result = _invoke_model(
        context,
        item,
        provider=provider,
        operation="reference_vision",
        actual_config=actual_config,
        input_artifacts=(("reference_input", input_artifact.artifact_id),),
        local_duration=None,
        retry_reason=retry_reason,
        call=lambda: provider.recognize(
            ReferenceVisionRequest((image,), "image_visual", manifest)
        ),
    )
    return _publish_model_evidence(
        context,
        reference,
        item,
        input_artifact=input_artifact,
        result=result,
        provider=provider.provider,
        model=provider.model,
        local_duration=None,
        content={"text": result.text},
        provenance={"image_manifest": manifest, "config": actual_config},
    )


def _execute_cloud_document(
    context: ProjectContext,
    reference: Mapping[str, Any],
    item: Mapping[str, Any],
    *,
    path: Path,
    detected_format: str,
    pool: _ProviderPool,
    retry_reason: str | None,
) -> ArtifactEnvelope:
    previous = _previous_reference_input(context, str(item["work_item_id"]))
    if previous is None:
        input_artifact = _publish_reference_input(
            context,
            reference,
            item,
            detected_format=detected_format,
            input_kind="cloud_document_upload",
            local_duration=None,
            manifest={
                "filename": str(reference["filename"]),
                "locator_at_run": str(path),
                "purpose": "file-extract",
            },
        )
    else:
        input_artifact = previous
    provider = pool.document()
    actual_config = cloud_document_actual_config()
    result = _invoke_model(
        context,
        item,
        provider=provider,
        operation="cloud_document_parse",
        actual_config=actual_config,
        input_artifacts=(("reference_input", input_artifact.artifact_id),),
        local_duration=None,
        retry_reason=retry_reason,
        call=lambda: provider.parse(CloudDocumentRequest(path, detected_format)),
    )
    return _publish_model_evidence(
        context,
        reference,
        item,
        input_artifact=input_artifact,
        result=result,
        provider=provider.provider,
        model=provider.model,
        local_duration=None,
        content={"text": result.text},
        provenance={
            "detected_format": detected_format,
            "remote_file_id_deleted": provider.last_cleanup_status == "deleted",
            "config": actual_config,
        },
    )


def _invoke_model(
    context: ProjectContext,
    item: Mapping[str, Any],
    *,
    provider: Any,
    operation: str,
    actual_config: Mapping[str, Any],
    input_artifacts: Sequence[tuple[str, str]],
    local_duration: float | None,
    retry_reason: str | None,
    call: Callable[[], ReferenceModelResult],
) -> _InvocationResult:
    work_item_id = str(item["work_item_id"])
    preflight = getattr(provider, "preflight", None)
    if callable(preflight):
        preflight()
    if context.registry.sent_reference_attempt_count(work_item_id) >= (
        REFERENCE_MODEL_SENT_ATTEMPT_LIMIT
    ):
        raise ContractError("Reference model work item sent-attempt budget is exhausted")
    previous_invocations = context.registry.reference_invocations_for_work_item(work_item_id)
    retry_parent = (
        str(previous_invocations[-1]["invocation_id"]) if previous_invocations else None
    )
    invocation_id = context.registry.create_reference_invocation(
        work_item_id=work_item_id,
        run_id=str(item["run_id"]),
        project_id=context.project_id,
        operation=operation,
        logical_operation_key=f"reference:{work_item_id}",
        attempt_number=len(previous_invocations) + 1,
        branch=str(item["branch"]),
        provider=str(provider.provider),
        model=str(provider.model),
        actual_config=actual_config,
        inputs=input_artifacts,
        local_measured_duration=local_duration,
        retry_parent_invocation_id=retry_parent,
        retry_reason=retry_reason,
        cleanup_status=("pending" if operation == "cloud_document_parse" else "not_applicable"),
    )
    context.registry.set_invocation_status(invocation_id, "sending")
    try:
        result = call()
    except Exception as exc:
        status = (
            "delivery_ambiguous"
            if isinstance(exc, DeliveryAmbiguousError)
            else "explicit_failure"
        )
        context.registry.set_invocation_status(
            invocation_id,
            status,
            error_message=str(exc),
        )
        partial_result = getattr(provider, "last_result", None)
        known_result = (
            partial_result if isinstance(partial_result, ReferenceModelResult) else None
        )
        context.registry.update_reference_invocation_details(
            invocation_id,
            response_id=known_result.response_id if known_result is not None else None,
            provider_usage_duration=(
                known_result.provider_usage_duration if known_result is not None else None
            ),
            provider_usage=(
                known_result.provider_usage if known_result is not None else None
            ),
            provider_cost=known_result.provider_cost if known_result is not None else None,
            failure_code=type(exc).__name__,
            failure_details={"message": str(exc)},
            remote_file_id=getattr(provider, "last_remote_file_id", None),
            cleanup_status=getattr(provider, "last_cleanup_status", None),
        )
        raise
    context.registry.update_reference_invocation_details(
        invocation_id,
        response_id=result.response_id,
        provider_usage_duration=result.provider_usage_duration,
        provider_usage=result.provider_usage,
        provider_cost=result.provider_cost,
        remote_file_id=getattr(provider, "last_remote_file_id", None),
        cleanup_status=getattr(provider, "last_cleanup_status", None),
    )
    return _InvocationResult(result, invocation_id)


def _publish_model_evidence(
    context: ProjectContext,
    reference: Mapping[str, Any],
    item: Mapping[str, Any],
    *,
    input_artifact: ArtifactEnvelope,
    result: _InvocationResult,
    provider: str,
    model: str,
    local_duration: float | None,
    content: Any,
    provenance: Mapping[str, Any],
) -> ArtifactEnvelope:
    try:
        evidence = _publish_reference_evidence(
            context,
            reference,
            item,
            input_artifact=input_artifact,
            content=content,
            provenance=provenance,
            local_duration=local_duration,
            result=result.value,
            provider=provider,
            model=model,
        )
    except Exception as exc:
        context.registry.set_invocation_status(
            result.invocation_id,
            "explicit_failure",
            response_id=result.value.response_id,
            error_message=f"Reference evidence publication failed: {exc}",
        )
        context.registry.update_reference_invocation_details(
            result.invocation_id,
            response_id=result.value.response_id,
            provider_usage_duration=result.value.provider_usage_duration,
            provider_usage=result.value.provider_usage,
            provider_cost=result.value.provider_cost,
            failure_code=type(exc).__name__,
            failure_details={"message": str(exc), "stage": "artifact_publication"},
        )
        raise
    context.registry.set_invocation_status(
        result.invocation_id,
        "succeeded",
        response_id=result.value.response_id,
        artifact_id=evidence.artifact_id,
    )
    return evidence


def _publish_reference_input(
    context: ProjectContext,
    reference: Mapping[str, Any],
    item: Mapping[str, Any],
    *,
    detected_format: str,
    input_kind: str,
    local_duration: float | None,
    manifest: Mapping[str, Any],
) -> ArtifactEnvelope:
    producer_config = {"input_kind": input_kind, "manifest": dict(manifest)}
    envelope = ArtifactEnvelope.create(
        artifact_kind="reference_input",
        scope_key=str(reference["reference_asset_id"]),
        producer=Producer(
            component="reference-input",
            component_version=COMPONENT_VERSION,
            provider=None,
            model=None,
            config_hash=hash_json(producer_config),
        ),
        inputs=(
            InputRef(
                role="reference_asset",
                reference_asset_id=str(reference["reference_asset_id"]),
            ),
        ),
        payload={
            "reference_asset_id": str(reference["reference_asset_id"]),
            "run_id": str(item["run_id"]),
            "work_item_id": str(item["work_item_id"]),
            "input_kind": input_kind,
            "branch": str(item["branch"]),
            "detected_format": detected_format,
            "local_measured_duration": local_duration,
            "manifest": dict(manifest),
        },
    )
    return context.publisher.publish(envelope, make_current=False)


def _publish_reference_evidence(
    context: ProjectContext,
    reference: Mapping[str, Any],
    item: Mapping[str, Any],
    *,
    input_artifact: ArtifactEnvelope,
    content: Any,
    provenance: Mapping[str, Any],
    local_duration: float | None,
    result: ReferenceModelResult | None,
    provider: str | None,
    model: str | None,
) -> ArtifactEnvelope:
    config = {"branch": str(item["branch"]), "provenance": dict(provenance)}
    envelope = ArtifactEnvelope.create(
        artifact_kind="reference_evidence",
        scope_key=str(reference["reference_asset_id"]),
        producer=Producer(
            component="reference-extraction",
            component_version=COMPONENT_VERSION,
            provider=provider,
            model=model,
            config_hash=hash_json(config),
        ),
        inputs=(InputRef(role="reference_input", artifact_id=input_artifact.artifact_id),),
        payload={
            "reference_asset_id": str(reference["reference_asset_id"]),
            "run_id": str(item["run_id"]),
            "work_item_id": str(item["work_item_id"]),
            "evidence_role": str(item["evidence_role"]),
            "branch": str(item["branch"]),
            "content": content,
            "provenance": dict(provenance),
            "local_measured_duration": local_duration,
            "provider_usage_duration": (
                result.provider_usage_duration if result is not None else None
            ),
            "provider_usage": (
                dict(result.provider_usage)
                if result and result.provider_usage is not None
                else None
            ),
            "provider_cost": result.provider_cost if result is not None else None,
        },
    )
    return context.publisher.publish(envelope, make_current=False)


def _previous_reference_input(
    context: ProjectContext, work_item_id: str
) -> ArtifactEnvelope | None:
    rows = context.registry.reference_invocations_for_work_item(work_item_id)
    for row in reversed(rows):
        for input_row in context.registry.invocation_inputs(str(row["invocation_id"])):
            if input_row["role"] == "reference_input":
                envelope = context.artifact(str(input_row["input_artifact_id"]))
                if envelope.artifact_kind != "reference_input":
                    raise ContractError("Reference Invocation input is not reference_input")
                return envelope
    for row in reversed(context.registry.reference_input_artifacts(context.project_id)):
        envelope = context.store.read_envelope(Path(str(row["storage_locator"])))
        if (
            envelope.artifact_kind == "reference_input"
            and envelope.payload.get("work_item_id") == work_item_id
        ):
            return envelope
    return None


def _finalize_reference_run(
    context: ProjectContext, run_id: str, *, publish_bundle: bool
) -> dict[str, Any]:
    items = [dict(row) for row in context.registry.reference_work_items_for_run(run_id)]
    successes = [row for row in items if row["status"] == "succeeded"]
    failures = [row for row in items if row["status"] != "succeeded"]
    if successes and not failures:
        status = "succeeded"
        outcome = "complete"
    elif successes:
        status = "failed"
        outcome = "partial"
    else:
        status = "failed"
        outcome = "failed"
    run = dict(context.registry.reference_run(run_id))
    bundle_artifact_id = (
        str(run["current_bundle_artifact_id"])
        if run["current_bundle_artifact_id"] is not None
        else None
    )
    if successes and publish_bundle:
        bundle = _publish_reference_bundle(context, run, successes, failures, outcome)
        bundle_artifact_id = bundle.artifact_id
    error_message = (
        None
        if not failures
        else f"{len(failures)} of {len(items)} Reference work items failed"
    )
    context.registry.set_reference_run_result(
        run_id,
        status=status,
        outcome=outcome,
        bundle_artifact_id=bundle_artifact_id,
        error_message=error_message,
    )
    return {
        "run_id": run_id,
        "status": status,
        "outcome": outcome,
        "reference_asset_id": str(run["reference_asset_id"]),
        "bundle_artifact_id": bundle_artifact_id,
        "work_items": [_work_item_status(context, row) for row in items],
    }


def _publish_reference_bundle(
    context: ProjectContext,
    run: Mapping[str, Any],
    successes: Sequence[Mapping[str, Any]],
    failures: Sequence[Mapping[str, Any]],
    outcome: str,
) -> ArtifactEnvelope:
    evidence_ids = [str(row["evidence_artifact_id"]) for row in successes]
    failure_payload = [
        {
            "work_item_id": str(row["work_item_id"]),
            "branch": str(row["branch"]),
            "evidence_role": str(row["evidence_role"]),
            "failure_code": row["failure_code"],
            "failure_details": _json_nullable_mapping(row["failure_details_json"]),
        }
        for row in failures
    ]
    envelope = ArtifactEnvelope.create(
        artifact_kind="reference_bundle",
        scope_key=str(run["reference_asset_id"]),
        producer=Producer(
            component="reference-bundle",
            component_version=COMPONENT_VERSION,
            provider=None,
            model=None,
            config_hash=hash_json(
                {"run_id": str(run["run_id"]), "evidence_artifact_ids": evidence_ids}
            ),
        ),
        inputs=tuple(
            InputRef(role=str(row["evidence_role"]), artifact_id=str(row["evidence_artifact_id"]))
            for row in successes
        ),
        payload={
            "reference_asset_id": str(run["reference_asset_id"]),
            "run_id": str(run["run_id"]),
            "outcome": outcome,
            "evidence_artifact_ids": evidence_ids,
            "failures": failure_payload,
        },
    )
    return context.publisher.publish(envelope, make_current=False)


def _interrupt_reference_run(context: ProjectContext, run_id: str) -> None:
    context.registry.finalize_interrupted_run(
        run_id,
        run_status="interrupted",
        error_message="Reference Orchestrator interrupted by user",
    )


def _persist_bitmap_manifest(
    context: ProjectContext, cues: BitmapCueSet
) -> dict[str, Any]:
    unique: list[dict[str, Any]] = []
    for bitmap in cues.unique_bitmaps:
        content_hash, byte_length, _ = context.store.publish_blob(bitmap.path)
        unique.append(
            {
                "raw_pixel_sha256": bitmap.raw_pixel_sha256,
                "width": bitmap.width,
                "height": bitmap.height,
                "blob": {
                    "content_hash": content_hash,
                    "byte_length": byte_length,
                    "media_type": "image/png",
                },
                "occurrences": [
                    {
                        "start_ms": occurrence.start_ms,
                        "end_ms": occurrence.end_ms,
                        "stream_index": occurrence.stream_index,
                        "packet_index": occurrence.packet_index,
                    }
                    for occurrence in bitmap.occurrences
                ],
            }
        )
    return {
        "codec": cues.codec,
        "skipped_clear_or_empty_count": cues.skipped_empty_count,
        "unique_bitmaps": unique,
        "command_profile": cues.command_profile,
    }


def _frame_manifest(window: FrameWindow) -> dict[str, Any]:
    return {
        "start_ms": window.start_ms,
        "end_ms": window.end_ms,
        "command_profile": _stable_ffmpeg_command(window.command),
        "frames": [
            {
                "frame_id": frame.frame_id,
                "source_timestamp_ms": frame.source_timestamp_ms,
                "encoded_sha256": frame.encoded_sha256,
            }
            for frame in window.frames
        ],
    }


def _stable_ffmpeg_command(command: Sequence[str]) -> list[str]:
    normalized = list(command)
    try:
        input_index = normalized.index("-i") + 1
    except ValueError:
        input_index = -1
    if 0 <= input_index < len(normalized):
        normalized[input_index] = "<reference_locator>"
    if normalized:
        normalized[-1] = "<temporary_output>"
    return normalized


def _work_item_status(context: ProjectContext, row: Mapping[str, Any]) -> dict[str, Any]:
    work_item_id = str(row["work_item_id"])
    return {
        **dict(row),
        "work_spec": _json_mapping(str(row["work_spec_json"]), "Reference work spec"),
        "failure_details": _json_nullable_mapping(row["failure_details_json"]),
        "invocations": [
            dict(invocation)
            for invocation in context.registry.reference_invocations_for_work_item(work_item_id)
        ],
    }


def _reference_run_config(
    pixel_subtitle_mode: Literal["burned", "none"] | None
) -> dict[str, Any]:
    return {
        "pixel_subtitle_mode": pixel_subtitle_mode,
        "reference_vision_model": REFERENCE_VISION_MODEL,
        "cloud_reference_asr_model": CLOUD_REFERENCE_ASR_MODEL,
        "cloud_document_model": CLOUD_DOCUMENT_MODEL,
        "sent_attempt_limit": REFERENCE_MODEL_SENT_ATTEMPT_LIMIT,
    }


def _local_duration_seconds(config: Mapping[str, Any]) -> float | None:
    value = config.get("local_measured_duration_ms")
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ContractError("invalid local Reference duration")
    return float(value) / 1000


def _int_config(config: Mapping[str, Any], name: str) -> int:
    value = config.get(name)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ContractError(f"Reference work config {name} must be an integer")
    return value


def _json_mapping(value: str, name: str) -> dict[str, Any]:
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ContractError(f"{name} is invalid JSON") from exc
    if not isinstance(parsed, dict):
        raise ContractError(f"{name} must be an object")
    return cast(dict[str, Any], parsed)


def _json_nullable_mapping(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    return _json_mapping(str(value), "Reference failure details")
