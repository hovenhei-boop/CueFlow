from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict
from pathlib import Path
from typing import Any

from cueflow.alignment import build_alignment_payload
from cueflow.atomizer import build_transcript_payload
from cueflow.canonical import hash_json
from cueflow.config import (
    COMPONENT_VERSION,
    PROFILES,
    FillerReviewConfig,
    QaRulesetConfig,
    RuntimeConfig,
    SegmenterConfig,
    result_config,
)
from cueflow.errors import (
    ContractError,
    CueFlowError,
    DeliveryAmbiguousError,
    ExportBlockedError,
    ProviderError,
    ProviderUnavailableError,
)
from cueflow.export import publish_srt
from cueflow.filler import (
    cloud_filler_review_payload,
    local_filler_review_payload,
    unavailable_cloud_filler_payload,
)
from cueflow.glossary import effective_glossary, glossary_payload
from cueflow.media import MediaBundle, prepare_media, probe_source
from cueflow.project import ProjectContext
from cueflow.providers import (
    CloudOmniSemanticTranscriber,
    ForcedAligner,
    LocalQwenForcedAligner,
    LocalQwenSemanticTranscriber,
    SemanticResult,
    SemanticTranscriber,
)
from cueflow.qa import (
    evaluate_semantic_attempts,
    possible_chunk_boundary_duplication,
    qa_payload,
    structural_issues,
)
from cueflow.schema import ArtifactEnvelope, InputRef, Producer, find_unaligned_atoms
from cueflow.segmentation import segment_subtitles

SemanticFactory = Callable[[str, RuntimeConfig], SemanticTranscriber]
AlignerFactory = Callable[[RuntimeConfig], ForcedAligner]


def initialize_project(root: Path, display_name: str, profile: str) -> ProjectContext:
    context = ProjectContext.create(root, display_name, profile)
    try:
        _publish_glossaries(context, [], [])
        return context
    except BaseException:
        context.close()
        raise


def set_project_glossary(context: ProjectContext, terms: Sequence[str]) -> ArtifactEnvelope:
    system = context.current_artifact("system_glossary")
    project_payload = glossary_payload(terms)
    effective_payload = effective_glossary(system.payload, project_payload)
    producer = _deterministic_producer("glossary", {"normalization": "0.1.0"})
    project = ArtifactEnvelope.create(
        artifact_kind="project_glossary",
        scope_key="global",
        producer=producer,
        inputs=[],
        payload=project_payload,
    )
    effective = ArtifactEnvelope.create(
        artifact_kind="effective_glossary",
        scope_key="global",
        producer=producer,
        inputs=[
            InputRef(role="system_glossary", artifact_id=system.artifact_id),
            InputRef(role="project_glossary", artifact_id=project.artifact_id),
        ],
        payload=effective_payload,
    )
    for item in (project, effective):
        context.publisher.publish(item, make_current=False)
    context.registry.activate_artifacts(
        context.project_id,
        [project.artifact_id, effective.artifact_id],
        stale_targets=[
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
    )
    return effective


def run_project(
    context: ProjectContext,
    media_path: Path,
    *,
    runtime: RuntimeConfig | None = None,
    semantic_factory: SemanticFactory | None = None,
    aligner_factory: AlignerFactory | None = None,
) -> dict[str, Any]:
    chosen_runtime = runtime or RuntimeConfig.detect()
    project_row = context.registry.project()
    profile = str(project_row["processing_profile"])
    source_asset = context.register_external_asset(media_path, asset_kind="media")
    probe = probe_source(media_path, chosen_runtime)
    context.registry.set_source_media_kind(
        context.project_id, str(source_asset["source_asset_id"]), probe.media_kind
    )
    context.verify_external_asset(str(source_asset["source_asset_id"]))
    config = result_config(profile, chosen_runtime)
    run_id = context.registry.create_run(
        context.project_id,
        {
            "source_asset_id": source_asset["source_asset_id"],
            "content_hash": source_asset["content_hash"],
            "storage_locator": source_asset["storage_locator"],
        },
        hash_json(config),
    )
    context.registry.set_run_status(run_id, "running")
    semantic_builder = semantic_factory or _default_semantic_factory
    aligner_builder = aligner_factory or (lambda value: LocalQwenForcedAligner(value))
    try:
        media = prepare_media(context, source_asset, probe, chosen_runtime)
        effective = context.current_artifact("effective_glossary")
        terms = list(effective.payload.get("terms", []))
        transcripts: list[ArtifactEnvelope] = []
        alignments: list[ArtifactEnvelope] = []
        semantic_issues: list[dict[str, Any]] = []
        for media_chunk in media.media_chunks:
            transcript, alignment, issues = _process_chunk(
                context,
                run_id=run_id,
                profile=profile,
                runtime=chosen_runtime,
                media_chunk=media_chunk,
                effective_glossary=effective,
                glossary_terms=terms,
                semantic_factory=semantic_builder,
                aligner_factory=aligner_builder,
            )
            transcripts.append(transcript)
            alignments.append(alignment)
            semantic_issues.extend(issues)
        subtitle, segment_warnings = _publish_subtitle(
            context,
            profile,
            media,
            effective,
            transcripts,
            alignments,
            terms,
        )
        structural = structural_issues(
            media.media_chunks,
            transcripts,
            alignments,
            subtitle,
            duration_ms=probe.duration_ms,
            registry=context.registry,
            project_id=context.project_id,
        )
        if structural:
            subtitle, segment_warnings = _publish_subtitle(
                context,
                profile,
                media,
                effective,
                transcripts,
                alignments,
                terms,
            )
            structural = structural_issues(
                media.media_chunks,
                transcripts,
                alignments,
                subtitle,
                duration_ms=probe.duration_ms,
                registry=context.registry,
                project_id=context.project_id,
            )
        boundary_warnings = possible_chunk_boundary_duplication(transcripts)
        all_issues = [*semantic_issues, *segment_warnings, *boundary_warnings, *structural]
        qa = _publish_qa(
            context,
            profile,
            media,
            effective,
            subtitle,
            transcripts,
            alignments,
            all_issues,
        )
        if qa.payload["result"] == "blocked":
            raise ExportBlockedError("structural QA remained blocked after one repair pass")
        filler = _publish_filler_review(
            context,
            run_id=run_id,
            profile=profile,
            subtitle=subtitle,
            qa=qa,
            transcripts=transcripts,
            alignments=alignments,
            duration_ms=probe.duration_ms,
        )
        render, output_path = publish_srt(
            context,
            chunk_plan=media.chunk_plan,
            transcripts=transcripts,
            alignments=alignments,
            subtitle=subtitle,
            qa=qa,
            filler_review=filler,
        )
        context.registry.set_run_status(run_id, "succeeded")
        return {
            "run_id": run_id,
            "status": "succeeded",
            "source_asset_id": source_asset["source_asset_id"],
            "srt_render_artifact_id": render.artifact_id,
            "output": str(output_path.resolve()),
            "warnings": [
                issue for issue in qa.payload["issues"] if issue["severity"] == "warning"
            ]
            + list(filler.payload.get("warnings", []))
            + (
                [{"code": "timeline_status_unverified"}]
                if probe.payload["timeline_status"] == "unverified"
                else []
            ),
        }
    except BaseException as exc:
        context.registry.set_run_status(run_id, "failed", str(exc))
        raise


def retry_invocation(
    context: ProjectContext,
    invocation_id: str,
    *,
    runtime: RuntimeConfig | None = None,
) -> dict[str, Any]:
    invocation = context.registry.invocation(invocation_id)
    if invocation["project_id"] != context.project_id:
        raise ContractError("Invocation belongs to a different project")
    if invocation["status"] not in {
        "definitely_not_sent",
        "delivery_ambiguous",
        "explicit_failure",
    }:
        raise ContractError("only a failed or ambiguous Invocation can be explicitly retried")
    run = context.registry.run(str(invocation["run_id"]))
    identity = json.loads(str(run["input_identity_json"]))
    return run_project(context, Path(str(identity["storage_locator"])), runtime=runtime)


def project_status(context: ProjectContext) -> dict[str, Any]:
    project = context.registry.project()
    latest = context.registry.latest_run(context.project_id)
    pointers = [
        {
            "artifact_kind": str(row["artifact_kind"]),
            "scope_key": str(row["scope_key"]),
            "artifact_id": str(row["artifact_id"]),
            "is_stale": bool(row["is_stale"]),
        }
        for row in context.registry.current_pointers(context.project_id)
    ]
    warnings: list[Any] = []
    for kind in ("qa", "filler_review", "media_probe"):
        try:
            artifact = context.current_artifact(kind)
        except CueFlowError:
            continue
        if kind == "qa":
            warnings.extend(
                item for item in artifact.payload.get("issues", []) if item["severity"] == "warning"
            )
        elif kind == "filler_review":
            warnings.extend(artifact.payload.get("warnings", []))
        elif artifact.payload.get("timeline_status") == "unverified":
            warnings.append({"code": "timeline_status_unverified"})
    return {
        "project_id": context.project_id,
        "display_name": str(project["display_name"]),
        "processing_profile": str(project["processing_profile"]),
        "latest_run": (
            {
                "run_id": str(latest["run_id"]),
                "status": str(latest["status"]),
                "error_message": latest["error_message"],
            }
            if latest is not None
            else None
        ),
        "current_artifacts": pointers,
        "warnings": warnings,
    }


def _process_chunk(
    context: ProjectContext,
    *,
    run_id: str,
    profile: str,
    runtime: RuntimeConfig,
    media_chunk: ArtifactEnvelope,
    effective_glossary: ArtifactEnvelope,
    glossary_terms: Sequence[str],
    semantic_factory: SemanticFactory,
    aligner_factory: AlignerFactory,
) -> tuple[ArtifactEnvelope, ArtifactEnvelope, list[dict[str, Any]]]:
    attempt_payloads: list[Mapping[str, Any]] = []
    attempt_artifacts: list[str] = []
    rework_context: str | None = None
    limit = QaRulesetConfig().semantic_attempt_limit
    for attempt_number in range(1, limit + 1):
        transcript = _semantic_attempt(
            context,
            run_id=run_id,
            profile=profile,
            runtime=runtime,
            media_chunk=media_chunk,
            effective_glossary=effective_glossary,
            glossary_terms=glossary_terms,
            attempt_number=attempt_number,
            rework_context=rework_context,
            factory=semantic_factory,
        )
        alignment = _alignment_with_repair(
            context,
            run_id=run_id,
            profile=profile,
            runtime=runtime,
            media_chunk=media_chunk,
            transcript=transcript,
            semantic_attempt_number=attempt_number,
            factory=aligner_factory,
        )
        context.registry.activate_artifacts(
            context.project_id,
            [transcript.artifact_id, alignment.artifact_id],
            stale_targets=[
                (kind, "global") for kind in ("subtitle", "qa", "filler_review", "srt_render")
            ],
        )
        attempt_payloads.append(transcript.payload)
        attempt_artifacts.append(transcript.artifact_id)
        decision = evaluate_semantic_attempts(attempt_payloads, glossary_terms)
        if decision.action == "accepted":
            issues = [_with_attempt_artifacts(item, attempt_artifacts) for item in decision.issues]
            return transcript, alignment, issues
        rework_context = decision.rework_context
    raise ContractError("semantic attempt loop exceeded its frozen limit")


def _semantic_attempt(
    context: ProjectContext,
    *,
    run_id: str,
    profile: str,
    runtime: RuntimeConfig,
    media_chunk: ArtifactEnvelope,
    effective_glossary: ArtifactEnvelope,
    glossary_terms: Sequence[str],
    attempt_number: int,
    rework_context: str | None,
    factory: SemanticFactory,
) -> ArtifactEnvelope:
    provider = factory(profile, runtime)
    chunk_id = str(media_chunk.payload["chunk_id"])
    invocation_id = context.registry.create_invocation(
        run_id=run_id,
        project_id=context.project_id,
        operation="semantic_transcription",
        logical_operation_key=f"semantic:{chunk_id}",
        attempt_number=attempt_number,
        provider=provider.provider,
        model=provider.model,
        chunk_id=chunk_id,
    )
    context.registry.set_invocation_status(invocation_id, "sending")
    audio_path = context.store.blob_path(str(media_chunk.payload["audio_blob"]["content_hash"]))
    try:
        result = provider.transcribe(
            audio_path, glossary_terms, rework_context=rework_context
        )
        transcript = _transcript_envelope(
            profile,
            provider,
            media_chunk,
            effective_glossary,
            result,
            rework_context,
        )
        context.publisher.publish(transcript, make_current=False)
    except ProviderUnavailableError as exc:
        context.registry.set_invocation_status(
            invocation_id, "definitely_not_sent", error_message=str(exc)
        )
        raise
    except DeliveryAmbiguousError as exc:
        context.registry.set_invocation_status(
            invocation_id, "delivery_ambiguous", error_message=str(exc)
        )
        raise
    except (ProviderError, ContractError) as exc:
        context.registry.set_invocation_status(
            invocation_id, "explicit_failure", error_message=str(exc)
        )
        raise
    finally:
        provider.close()
    context.registry.set_invocation_status(
        invocation_id,
        "succeeded",
        response_id=result.response_id,
        artifact_id=transcript.artifact_id,
    )
    return transcript


def _alignment_with_repair(
    context: ProjectContext,
    *,
    run_id: str,
    profile: str,
    runtime: RuntimeConfig,
    media_chunk: ArtifactEnvelope,
    transcript: ArtifactEnvelope,
    semantic_attempt_number: int,
    factory: AlignerFactory,
) -> ArtifactEnvelope:
    attempts = 1 + QaRulesetConfig().structural_repair_limit
    last: ArtifactEnvelope | None = None
    for repair_number in range(1, attempts + 1):
        aligner = factory(runtime)
        chunk_id = str(media_chunk.payload["chunk_id"])
        invocation_id = context.registry.create_invocation(
            run_id=run_id,
            project_id=context.project_id,
            operation="forced_alignment",
            logical_operation_key=f"alignment:{chunk_id}:semantic:{semantic_attempt_number}",
            attempt_number=repair_number,
            provider=aligner.provider,
            model=aligner.model,
            chunk_id=chunk_id,
        )
        context.registry.set_invocation_status(invocation_id, "sending")
        audio_path = context.store.blob_path(
            str(media_chunk.payload["audio_blob"]["content_hash"])
        )
        try:
            tokens = aligner.align(
                audio_path,
                str(transcript.payload["source_text"]),
                transcript.payload.get("language"),
            )
            payload = build_alignment_payload(
                media_chunk_artifact_id=media_chunk.artifact_id,
                media_chunk=media_chunk.payload,
                transcript_artifact_id=transcript.artifact_id,
                transcript=transcript.payload,
                tokens=tokens,
            )
            producer = Producer(
                component="alignment",
                component_version=COMPONENT_VERSION,
                processing_profile=profile,
                provider=aligner.provider,
                model=aligner.model,
                config_hash=hash_json(
                    {
                        "revision": aligner.revision,
                        "runtime_device": asdict(runtime.device),
                    }
                ),
            )
            last = ArtifactEnvelope.create(
                artifact_kind="alignment",
                scope_key=chunk_id,
                producer=producer,
                inputs=[
                    InputRef(role="media_chunk", artifact_id=media_chunk.artifact_id),
                    InputRef(role="transcript", artifact_id=transcript.artifact_id),
                ],
                payload=payload,
            )
            context.publisher.publish(last, make_current=False)
        except ProviderUnavailableError as exc:
            context.registry.set_invocation_status(
                invocation_id, "definitely_not_sent", error_message=str(exc)
            )
            raise
        except DeliveryAmbiguousError as exc:
            context.registry.set_invocation_status(
                invocation_id, "delivery_ambiguous", error_message=str(exc)
            )
            raise
        except (ProviderError, ContractError) as exc:
            context.registry.set_invocation_status(
                invocation_id, "explicit_failure", error_message=str(exc)
            )
            raise
        finally:
            aligner.close()
        context.registry.set_invocation_status(
            invocation_id, "succeeded", artifact_id=last.artifact_id
        )
        if not find_unaligned_atoms(last.payload):
            return last
    if last is None:
        raise ContractError("Alignment did not produce an Artifact")
    context.registry.activate_artifacts(
        context.project_id,
        [transcript.artifact_id, last.artifact_id],
        stale_targets=[
            (kind, "global") for kind in ("subtitle", "qa", "filler_review", "srt_render")
        ],
    )
    raise ExportBlockedError("real-sound Atoms remained unaligned after one repair pass")


def _transcript_envelope(
    profile: str,
    provider: SemanticTranscriber,
    media_chunk: ArtifactEnvelope,
    effective_glossary: ArtifactEnvelope,
    result: SemanticResult,
    rework_context: str | None,
) -> ArtifactEnvelope:
    payload = build_transcript_payload(
        chunk_id=str(media_chunk.payload["chunk_id"]),
        source_text=result.source_text,
        language=result.language,
        semantic_confidence=(
            result.semantic_confidence
            if result.semantic_confidence is not None
            else {"availability": "unavailable", "provider": provider.provider}
        ),
        provider_uncertain_spans=result.provider_uncertain_spans,
    )
    payload["provider_model_revision"] = provider.revision
    producer = Producer(
        component="semantic_transcriber",
        component_version=COMPONENT_VERSION,
        processing_profile=profile,
        provider=provider.provider,
        model=provider.model,
        config_hash=hash_json(
            {
                "revision": provider.revision,
                "effective_glossary_artifact_id": effective_glossary.artifact_id,
                "rework_context": rework_context,
                "verbatim_transcription": True,
            }
        ),
    )
    return ArtifactEnvelope.create(
        artifact_kind="transcript",
        scope_key=str(media_chunk.payload["chunk_id"]),
        producer=producer,
        inputs=[
            InputRef(role="media_chunk", artifact_id=media_chunk.artifact_id),
            InputRef(role="effective_glossary", artifact_id=effective_glossary.artifact_id),
        ],
        payload=payload,
    )


def _publish_subtitle(
    context: ProjectContext,
    profile: str,
    media: MediaBundle,
    effective_glossary: ArtifactEnvelope,
    transcripts: Sequence[ArtifactEnvelope],
    alignments: Sequence[ArtifactEnvelope],
    terms: Sequence[str],
) -> tuple[ArtifactEnvelope, list[dict[str, Any]]]:
    payload, warnings = segment_subtitles(
        transcripts,
        alignments,
        terms,
        duration_ms=int(media.chunk_plan.payload["duration_ms"]),
    )
    producer = Producer(
        component="segmenter",
        component_version=COMPONENT_VERSION,
        processing_profile=profile,
        provider=None,
        model=None,
        config_hash=hash_json(asdict(SegmenterConfig())),
    )
    inputs = [
        *[InputRef(role="transcript", artifact_id=item.artifact_id) for item in transcripts],
        *[InputRef(role="alignment", artifact_id=item.artifact_id) for item in alignments],
        InputRef(role="effective_glossary", artifact_id=effective_glossary.artifact_id),
    ]
    envelope = ArtifactEnvelope.create(
        artifact_kind="subtitle",
        scope_key="global",
        producer=producer,
        inputs=inputs,
        payload=payload,
    )
    context.publisher.publish(
        envelope,
        stale_targets=[("qa", "global"), ("filler_review", "global"), ("srt_render", "global")],
    )
    return envelope, warnings


def _publish_qa(
    context: ProjectContext,
    profile: str,
    media: MediaBundle,
    effective_glossary: ArtifactEnvelope,
    subtitle: ArtifactEnvelope,
    transcripts: Sequence[ArtifactEnvelope],
    alignments: Sequence[ArtifactEnvelope],
    issues: Sequence[Mapping[str, Any]],
) -> ArtifactEnvelope:
    subjects = [
        media.chunk_plan.artifact_id,
        subtitle.artifact_id,
        effective_glossary.artifact_id,
        *[item.artifact_id for item in transcripts],
        *[item.artifact_id for item in alignments],
    ]
    payload = qa_payload(subjects, issues)
    producer = Producer(
        component="qa",
        component_version=COMPONENT_VERSION,
        processing_profile=profile,
        provider=None,
        model=None,
        config_hash=hash_json(asdict(QaRulesetConfig())),
    )
    envelope = ArtifactEnvelope.create(
        artifact_kind="qa",
        scope_key="global",
        producer=producer,
        inputs=[
            InputRef(role="chunk_plan", artifact_id=media.chunk_plan.artifact_id),
            InputRef(role="subtitle", artifact_id=subtitle.artifact_id),
            InputRef(
                role="effective_glossary", artifact_id=effective_glossary.artifact_id
            ),
            *[InputRef(role="transcript", artifact_id=item.artifact_id) for item in transcripts],
            *[InputRef(role="alignment", artifact_id=item.artifact_id) for item in alignments],
        ],
        payload=payload,
    )
    context.publisher.publish(
        envelope,
        stale_targets=[("filler_review", "global"), ("srt_render", "global")],
    )
    return envelope


def _publish_filler_review(
    context: ProjectContext,
    *,
    run_id: str,
    profile: str,
    subtitle: ArtifactEnvelope,
    qa: ArtifactEnvelope,
    transcripts: Sequence[ArtifactEnvelope],
    alignments: Sequence[ArtifactEnvelope],
    duration_ms: int,
) -> ArtifactEnvelope:
    response_id: str | None = None
    invocation_id: str | None = None
    if profile == "LOCAL_PROFILE":
        payload = local_filler_review_payload(
            subtitle.artifact_id, subtitle.payload, duration_ms=duration_ms
        )
        provider = None
        model = None
    else:
        provider = "dashscope-openai-compatible"
        model = PROFILES[profile].semantic_model
        invocation_id = context.registry.create_invocation(
            run_id=run_id,
            project_id=context.project_id,
            operation="filler_review",
            logical_operation_key=f"filler:{subtitle.artifact_id}",
            attempt_number=1,
            provider=provider,
            model=model,
        )
        context.registry.set_invocation_status(invocation_id, "sending")
        try:
            payload, response_id = cloud_filler_review_payload(
                subtitle.artifact_id, subtitle.payload, duration_ms=duration_ms
            )
        except ProviderUnavailableError as exc:
            context.registry.set_invocation_status(
                invocation_id, "definitely_not_sent", error_message=str(exc)
            )
            payload = unavailable_cloud_filler_payload(
                subtitle.artifact_id,
                subtitle.payload,
                duration_ms=duration_ms,
                reason="definitely_not_sent",
            )
        except DeliveryAmbiguousError as exc:
            context.registry.set_invocation_status(
                invocation_id, "delivery_ambiguous", error_message=str(exc)
            )
            payload = unavailable_cloud_filler_payload(
                subtitle.artifact_id,
                subtitle.payload,
                duration_ms=duration_ms,
                reason="delivery_ambiguous",
            )
        except (ProviderError, ContractError) as exc:
            context.registry.set_invocation_status(
                invocation_id, "explicit_failure", error_message=str(exc)
            )
            payload = unavailable_cloud_filler_payload(
                subtitle.artifact_id,
                subtitle.payload,
                duration_ms=duration_ms,
                reason="invalid_or_explicit_failure",
            )
    producer = Producer(
        component="filler_review",
        component_version=COMPONENT_VERSION,
        processing_profile=profile,
        provider=provider,
        model=model,
        config_hash=hash_json(asdict(FillerReviewConfig())),
    )
    envelope = ArtifactEnvelope.create(
        artifact_kind="filler_review",
        scope_key="global",
        producer=producer,
        inputs=[
            InputRef(role="subtitle", artifact_id=subtitle.artifact_id),
            InputRef(role="qa", artifact_id=qa.artifact_id),
            *[
                InputRef(role="transcript", artifact_id=item.artifact_id)
                for item in transcripts
            ],
            *[
                InputRef(role="alignment", artifact_id=item.artifact_id)
                for item in alignments
            ],
        ],
        payload=payload,
    )
    context.publisher.publish(envelope, stale_targets=[("srt_render", "global")])
    if invocation_id is not None and payload["status"] == "completed":
        context.registry.set_invocation_status(
            invocation_id,
            "succeeded",
            response_id=response_id,
            artifact_id=envelope.artifact_id,
        )
    return envelope


def _publish_glossaries(
    context: ProjectContext, system_terms: Sequence[str], project_terms: Sequence[str]
) -> None:
    producer = _deterministic_producer("glossary", {"normalization": "0.1.0"})
    system = ArtifactEnvelope.create(
        artifact_kind="system_glossary",
        scope_key="global",
        producer=producer,
        inputs=[],
        payload=glossary_payload(system_terms),
    )
    project = ArtifactEnvelope.create(
        artifact_kind="project_glossary",
        scope_key="global",
        producer=producer,
        inputs=[],
        payload=glossary_payload(project_terms),
    )
    effective = ArtifactEnvelope.create(
        artifact_kind="effective_glossary",
        scope_key="global",
        producer=producer,
        inputs=[
            InputRef(role="system_glossary", artifact_id=system.artifact_id),
            InputRef(role="project_glossary", artifact_id=project.artifact_id),
        ],
        payload=effective_glossary(system.payload, project.payload),
    )
    for item in (system, project, effective):
        context.publisher.publish(item, make_current=False)
    context.registry.activate_artifacts(
        context.project_id, [system.artifact_id, project.artifact_id, effective.artifact_id]
    )


def _deterministic_producer(component: str, config: Mapping[str, Any]) -> Producer:
    return Producer(
        component=component,
        component_version=COMPONENT_VERSION,
        processing_profile=None,
        provider=None,
        model=None,
        config_hash=hash_json(config),
    )


def _default_semantic_factory(profile: str, runtime: RuntimeConfig) -> SemanticTranscriber:
    if profile == "LOCAL_PROFILE":
        return LocalQwenSemanticTranscriber(runtime)
    return CloudOmniSemanticTranscriber()


def _with_attempt_artifacts(
    issue: Mapping[str, Any], artifact_ids: Sequence[str]
) -> dict[str, Any]:
    value = dict(issue)
    value["replacement_artifact_ids"] = list(artifact_ids[1:])
    return value
