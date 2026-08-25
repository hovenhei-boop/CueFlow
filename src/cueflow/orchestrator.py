from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict
from pathlib import Path
from typing import Any

from cueflow.alignment import build_alignment_payload
from cueflow.atomizer import build_transcript_payload
from cueflow.canonical import hash_json
from cueflow.config import (
    COMPONENT_VERSION,
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
    alignment_repair_workset,
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
            for kind in ("transcript", "alignment", "subtitle", "qa", "srt_render")
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
    context.registry.recover_running_runs()
    chosen_runtime = runtime or RuntimeConfig.detect()
    profile = str(context.registry.project()["processing_profile"])
    source_asset = context.register_external_asset(media_path, asset_kind="media")
    run_id = context.registry.create_run(
        context.project_id,
        {
            "source_asset_id": source_asset["source_asset_id"],
            "filename": source_asset["filename"],
        },
        hash_json(result_config(profile, chosen_runtime)),
    )
    try:
        context.registry.set_run_status(run_id, "running")
        source_path = context.verify_external_asset(str(source_asset["source_asset_id"]))
        probe = probe_source(source_path, chosen_runtime)
        context.registry.set_source_media_kind(
            context.project_id, str(source_asset["source_asset_id"]), probe.media_kind
        )
        media = prepare_media(context, source_asset, probe, chosen_runtime)
        effective = context.current_artifact("effective_glossary")
        return _execute_existing_media_run(
            context,
            run_id=run_id,
            profile=profile,
            runtime=chosen_runtime,
            media=media,
            effective_glossary=effective,
            semantic_factory=semantic_factory or _default_semantic_factory,
            aligner_factory=aligner_factory or _default_aligner_factory,
        )
    except BaseException as exc:
        context.registry.finalize_interrupted_run(
            run_id,
            run_status="interrupted" if isinstance(exc, KeyboardInterrupt) else "failed",
            error_message=str(exc) or type(exc).__name__,
        )
        raise


def retry_invocation(
    context: ProjectContext,
    invocation_id: str,
    *,
    runtime: RuntimeConfig | None = None,
    semantic_factory: SemanticFactory | None = None,
    aligner_factory: AlignerFactory | None = None,
) -> dict[str, Any]:
    context.registry.recover_running_runs()
    invocation = context.registry.invocation(invocation_id)
    if invocation["project_id"] != context.project_id:
        raise ContractError("Invocation belongs to a different project")
    if invocation["status"] not in {
        "definitely_not_sent",
        "delivery_ambiguous",
        "explicit_failure",
    }:
        raise ContractError("only a failed or ambiguous Invocation can be explicitly retried")
    operation = str(invocation["operation"])
    if operation not in {"semantic_transcription", "forced_alignment", "qa_alignment_repair"}:
        raise ContractError("Invocation operation is not a retryable v0.1.1 operation")
    run_id = str(invocation["run_id"])
    run = context.registry.run(run_id)
    if run["status"] not in {"failed", "interrupted"}:
        raise ContractError("targeted retry requires a failed or interrupted Run")
    bound_inputs = _bound_inputs(context, invocation_id)
    media, effective, bound_transcript = _retry_graph(context, bound_inputs)
    chunk_id = str(invocation["chunk_id"])
    try:
        if (
            operation == "semantic_transcription"
            and invocation["status"] != "definitely_not_sent"
        ):
            context.registry.record_semantic_budget_reset(
                run_id, context.project_id, chunk_id, invocation_id
            )
        context.registry.reopen_run_for_retry(run_id)
        chosen_runtime = runtime or RuntimeConfig.detect()
        profile = str(context.registry.project()["processing_profile"])
        return _execute_existing_media_run(
            context,
            run_id=run_id,
            profile=profile,
            runtime=chosen_runtime,
            media=media,
            effective_glossary=effective,
            semantic_factory=semantic_factory or _default_semantic_factory,
            aligner_factory=aligner_factory or _default_aligner_factory,
            force_semantic_chunks=(
                {chunk_id} if operation == "semantic_transcription" else set()
            ),
            force_alignment_chunks=(
                {chunk_id} if operation in {"forced_alignment", "qa_alignment_repair"} else set()
            ),
            bound_transcript=bound_transcript,
            explicit_alignment_retry=operation in {"forced_alignment", "qa_alignment_repair"},
            forced_alignment_operation=(
                operation
                if operation in {"forced_alignment", "qa_alignment_repair"}
                else "forced_alignment"
            ),
        )
    except BaseException as exc:
        context.registry.finalize_interrupted_run(
            run_id,
            run_status="interrupted" if isinstance(exc, KeyboardInterrupt) else "failed",
            error_message=str(exc) or type(exc).__name__,
        )
        raise


def project_status(context: ProjectContext) -> dict[str, Any]:
    project = context.registry.project()
    latest = context.registry.latest_source_run(context.project_id)
    reference_runs = [
        {
            "run_id": str(row["run_id"]),
            "reference_asset_id": str(row["reference_asset_id"]),
            "status": str(row["status"]),
            "outcome": row["outcome"],
            "error_message": row["error_message"],
        }
        for row in context.registry.reference_runs()
    ]
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
    for kind in ("qa", "media_probe"):
        try:
            artifact = context.current_artifact(kind)
        except CueFlowError:
            continue
        if kind == "qa":
            warnings.extend(
                item for item in artifact.payload.get("issues", []) if item["severity"] == "warning"
            )
        elif artifact.payload.get("timeline_status") == "unverified":
            warnings.append({"code": "timeline_status_unverified"})
    return {
        "project_id": context.project_id,
        "display_name": str(project["display_name"]),
        "processing_profile": str(project["processing_profile"]),
        "latest_source_run": (
            {
                "run_id": str(latest["run_id"]),
                "status": str(latest["status"]),
                "error_message": latest["error_message"],
            }
            if latest is not None
            else None
        ),
        "reference_runs": reference_runs,
        "current_artifacts": pointers,
        "warnings": warnings,
    }


def _execute_existing_media_run(
    context: ProjectContext,
    *,
    run_id: str,
    profile: str,
    runtime: RuntimeConfig,
    media: MediaBundle,
    effective_glossary: ArtifactEnvelope,
    semantic_factory: SemanticFactory,
    aligner_factory: AlignerFactory,
    force_semantic_chunks: set[str] | None = None,
    force_alignment_chunks: set[str] | None = None,
    bound_transcript: ArtifactEnvelope | None = None,
    explicit_alignment_retry: bool = False,
    forced_alignment_operation: str = "forced_alignment",
) -> dict[str, Any]:
    transcripts, semantic_issues = _run_transcription_stage(
        context,
        run_id=run_id,
        profile=profile,
        runtime=runtime,
        media_chunks=media.media_chunks,
        effective_glossary=effective_glossary,
        semantic_factory=semantic_factory,
        force_chunks=force_semantic_chunks or set(),
    )
    if bound_transcript is not None:
        transcript_by_chunk = {
            str(item.payload["chunk_id"]): item for item in transcripts
        }
        transcript_by_chunk[str(bound_transcript.payload["chunk_id"])] = bound_transcript
        transcripts = tuple(
            transcript_by_chunk[str(chunk.payload["chunk_id"])]
            for chunk in media.media_chunks
        )
        context.publisher.publish(bound_transcript, make_current=True)
    alignments = _run_alignment_stage(
        context,
        run_id=run_id,
        profile=profile,
        runtime=runtime,
        media_chunks=media.media_chunks,
        transcripts=transcripts,
        aligner_factory=aligner_factory,
        force_chunks=force_alignment_chunks or set(),
        explicit_retry=explicit_alignment_retry,
        forced_operation=forced_alignment_operation,
    )
    return _complete_downstream(
        context,
        run_id=run_id,
        profile=profile,
        runtime=runtime,
        media=media,
        effective_glossary=effective_glossary,
        transcripts=transcripts,
        alignments=alignments,
        semantic_issues=semantic_issues,
        aligner_factory=aligner_factory,
    )


def _run_transcription_stage(
    context: ProjectContext,
    *,
    run_id: str,
    profile: str,
    runtime: RuntimeConfig,
    media_chunks: Sequence[ArtifactEnvelope],
    effective_glossary: ArtifactEnvelope,
    semantic_factory: SemanticFactory,
    force_chunks: set[str],
) -> tuple[tuple[ArtifactEnvelope, ...], list[dict[str, Any]]]:
    accepted: dict[str, ArtifactEnvelope] = {}
    issues: list[dict[str, Any]] = []
    work: list[ArtifactEnvelope] = []
    for chunk in media_chunks:
        chunk_id = str(chunk.payload["chunk_id"])
        existing = None if chunk_id in force_chunks else _accepted_transcript_for_run(
            context, run_id, chunk_id
        )
        if existing is None:
            work.append(chunk)
        else:
            accepted[chunk_id] = existing
            issues.extend(
                _semantic_issues_for_accepted(
                    context, run_id, chunk_id, existing, effective_glossary
                )
            )
    if work:
        provider = semantic_factory(profile, runtime)
        try:
            for chunk in work:
                transcript, chunk_issues = _semantic_attempts_for_chunk(
                    context,
                    run_id=run_id,
                    profile=profile,
                    media_chunk=chunk,
                    effective_glossary=effective_glossary,
                    provider=provider,
                )
                accepted[str(chunk.payload["chunk_id"])] = transcript
                issues.extend(chunk_issues)
        finally:
            provider.close()
    return (
        tuple(accepted[str(chunk.payload["chunk_id"])] for chunk in media_chunks),
        issues,
    )


def _semantic_attempts_for_chunk(
    context: ProjectContext,
    *,
    run_id: str,
    profile: str,
    media_chunk: ArtifactEnvelope,
    effective_glossary: ArtifactEnvelope,
    provider: SemanticTranscriber,
) -> tuple[ArtifactEnvelope, list[dict[str, Any]]]:
    chunk_id = str(media_chunk.payload["chunk_id"])
    terms = [str(item) for item in effective_glossary.payload.get("terms", [])]
    window = context.registry.semantic_budget_window(run_id, chunk_id)
    attempts = _successful_semantic_attempts(context, run_id, chunk_id, window)
    attempt_payloads = [item.payload for item in attempts]
    rework_context: str | None = None
    if attempt_payloads:
        decision = evaluate_semantic_attempts(attempt_payloads, terms)
        if decision.action == "accepted":
            transcript = attempts[-1]
            _activate_transcript(context, transcript)
            return transcript, [
                _with_attempt_artifacts(item, [attempt.artifact_id for attempt in attempts])
                for item in decision.issues
            ]
        rework_context = decision.rework_context
    config = QaRulesetConfig()
    while (
        context.registry.sent_semantic_attempt_count(run_id, chunk_id, window)
        < config.semantic_attempt_limit
    ):
        transcript = _semantic_attempt(
            context,
            run_id=run_id,
            profile=profile,
            media_chunk=media_chunk,
            effective_glossary=effective_glossary,
            glossary_terms=terms,
            budget_window=window,
            rework_context=rework_context,
            provider=provider,
        )
        attempts.append(transcript)
        attempt_payloads.append(transcript.payload)
        decision = evaluate_semantic_attempts(attempt_payloads, terms)
        if decision.action == "accepted":
            _activate_transcript(context, transcript)
            return transcript, [
                _with_attempt_artifacts(item, [attempt.artifact_id for attempt in attempts])
                for item in decision.issues
            ]
        rework_context = decision.rework_context
    raise ContractError("semantic attempt budget exhausted without an accepted Transcript")


def _semantic_attempt(
    context: ProjectContext,
    *,
    run_id: str,
    profile: str,
    media_chunk: ArtifactEnvelope,
    effective_glossary: ArtifactEnvelope,
    glossary_terms: Sequence[str],
    budget_window: int,
    rework_context: str | None,
    provider: SemanticTranscriber,
) -> ArtifactEnvelope:
    chunk_id = str(media_chunk.payload["chunk_id"])
    logical_key = f"semantic:{chunk_id}"
    invocation_id = context.registry.create_invocation(
        run_id=run_id,
        project_id=context.project_id,
        operation="semantic_transcription",
        logical_operation_key=logical_key,
        attempt_number=context.registry.next_invocation_attempt_number(run_id, logical_key),
        semantic_budget_window=budget_window,
        provider=provider.provider,
        model=provider.model,
        chunk_id=chunk_id,
        inputs=[
            ("media_chunk", media_chunk.artifact_id),
            ("effective_glossary", effective_glossary.artifact_id),
        ],
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
    context.registry.set_invocation_status(
        invocation_id,
        "succeeded",
        response_id=result.response_id,
        artifact_id=transcript.artifact_id,
    )
    return transcript


def _run_alignment_stage(
    context: ProjectContext,
    *,
    run_id: str,
    profile: str,
    runtime: RuntimeConfig,
    media_chunks: Sequence[ArtifactEnvelope],
    transcripts: Sequence[ArtifactEnvelope],
    aligner_factory: AlignerFactory,
    force_chunks: set[str],
    explicit_retry: bool,
    forced_operation: str,
) -> tuple[ArtifactEnvelope, ...]:
    if forced_operation not in {"forced_alignment", "qa_alignment_repair"}:
        raise ContractError("invalid forced alignment operation")
    transcript_by_chunk = {str(item.payload["chunk_id"]): item for item in transcripts}
    accepted: dict[str, ArtifactEnvelope] = {}
    work: list[tuple[ArtifactEnvelope, ArtifactEnvelope]] = []
    for chunk in media_chunks:
        chunk_id = str(chunk.payload["chunk_id"])
        transcript = transcript_by_chunk[chunk_id]
        existing = None if chunk_id in force_chunks else _successful_alignment_for_run(
            context, run_id, chunk, transcript
        )
        if existing is None:
            work.append((chunk, transcript))
        else:
            accepted[chunk_id] = existing
    if work:
        aligner = aligner_factory(runtime)
        try:
            for chunk, transcript in work:
                chunk_id = str(chunk.payload["chunk_id"])
                repair_limit = (
                    0
                    if explicit_retry and chunk_id in force_chunks
                    else QaRulesetConfig().alignment_structural_repair_limit
                )
                accepted[chunk_id] = _alignment_with_structural_repair(
                    context,
                    run_id=run_id,
                    profile=profile,
                    runtime=runtime,
                    media_chunk=chunk,
                    transcript=transcript,
                    aligner=aligner,
                    repair_limit=repair_limit,
                    operation=(
                        forced_operation if chunk_id in force_chunks else "forced_alignment"
                    ),
                )
        finally:
            aligner.close()
    ordered = tuple(accepted[str(chunk.payload["chunk_id"])] for chunk in media_chunks)
    context.registry.activate_artifacts(
        context.project_id,
        [item.artifact_id for item in ordered],
        stale_targets=[(kind, "global") for kind in ("subtitle", "qa", "srt_render")],
    )
    return ordered


def _alignment_with_structural_repair(
    context: ProjectContext,
    *,
    run_id: str,
    profile: str,
    runtime: RuntimeConfig,
    media_chunk: ArtifactEnvelope,
    transcript: ArtifactEnvelope,
    aligner: ForcedAligner,
    repair_limit: int,
    operation: str,
) -> ArtifactEnvelope:
    last_error = "alignment result remained structurally invalid"
    for _ in range(1 + repair_limit):
        try:
            alignment, valid = _alignment_attempt(
                context,
                run_id=run_id,
                profile=profile,
                runtime=runtime,
                media_chunk=media_chunk,
                transcript=transcript,
                aligner=aligner,
                operation=operation,
            )
        except ContractError as exc:
            last_error = str(exc)
            continue
        if valid:
            return alignment
        last_error = "real-sound Atoms remained unaligned"
    raise ExportBlockedError(last_error + " after the allowed structural repair")


def _alignment_attempt(
    context: ProjectContext,
    *,
    run_id: str,
    profile: str,
    runtime: RuntimeConfig,
    media_chunk: ArtifactEnvelope,
    transcript: ArtifactEnvelope,
    aligner: ForcedAligner,
    operation: str,
) -> tuple[ArtifactEnvelope, bool]:
    chunk_id = str(media_chunk.payload["chunk_id"])
    logical_key = f"{operation}:{chunk_id}:{transcript.artifact_id}"
    invocation_id = context.registry.create_invocation(
        run_id=run_id,
        project_id=context.project_id,
        operation=operation,
        logical_operation_key=logical_key,
        attempt_number=context.registry.next_invocation_attempt_number(run_id, logical_key),
        provider=aligner.provider,
        model=aligner.model,
        chunk_id=chunk_id,
        inputs=[
            ("media_chunk", media_chunk.artifact_id),
            ("transcript", transcript.artifact_id),
        ],
    )
    context.registry.set_invocation_status(invocation_id, "sending")
    audio_path = context.store.blob_path(str(media_chunk.payload["audio_blob"]["content_hash"]))
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
                {"revision": aligner.revision, "runtime_device": asdict(runtime.device)}
            ),
        )
        alignment = ArtifactEnvelope.create(
            artifact_kind="alignment",
            scope_key=chunk_id,
            producer=producer,
            inputs=[
                InputRef(role="media_chunk", artifact_id=media_chunk.artifact_id),
                InputRef(role="transcript", artifact_id=transcript.artifact_id),
            ],
            payload=payload,
        )
        context.publisher.publish(alignment, make_current=False)
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
    unaligned = find_unaligned_atoms(alignment.payload)
    if unaligned:
        context.registry.set_invocation_status(
            invocation_id,
            "explicit_failure",
            artifact_id=alignment.artifact_id,
            error_message=f"unaligned real-sound atoms: {unaligned}",
        )
        return alignment, False
    context.registry.set_invocation_status(
        invocation_id, "succeeded", artifact_id=alignment.artifact_id
    )
    return alignment, True


def _complete_downstream(
    context: ProjectContext,
    *,
    run_id: str,
    profile: str,
    runtime: RuntimeConfig,
    media: MediaBundle,
    effective_glossary: ArtifactEnvelope,
    transcripts: Sequence[ArtifactEnvelope],
    alignments: Sequence[ArtifactEnvelope],
    semantic_issues: Sequence[Mapping[str, Any]],
    aligner_factory: AlignerFactory,
) -> dict[str, Any]:
    terms = [str(item) for item in effective_glossary.payload.get("terms", [])]
    subtitle, segment_warnings = _publish_subtitle(
        context, profile, media, effective_glossary, transcripts, alignments, terms
    )
    structural = _structural_issues(context, media, transcripts, alignments, subtitle)
    issues = [
        *semantic_issues,
        *segment_warnings,
        *possible_chunk_boundary_duplication(transcripts),
        *structural,
    ]
    qa = _publish_qa(
        context,
        profile,
        media,
        effective_glossary,
        subtitle,
        transcripts,
        alignments,
        issues,
    )
    if qa.payload["result"] == "blocked":
        workset = alignment_repair_workset(structural)
        if workset:
            if (
                context.registry.qa_repair_wave_count(run_id)
                >= QaRulesetConfig().qa_alignment_repair_wave_limit
            ):
                raise ExportBlockedError("QA Alignment Repair Wave limit exhausted")
            alignments = _qa_alignment_repair_wave(
                context,
                run_id=run_id,
                profile=profile,
                runtime=runtime,
                media_chunks=media.media_chunks,
                transcripts=transcripts,
                alignments=alignments,
                workset=workset,
                aligner_factory=aligner_factory,
            )
        subtitle, segment_warnings = _publish_subtitle(
            context, profile, media, effective_glossary, transcripts, alignments, terms
        )
        structural = _structural_issues(context, media, transcripts, alignments, subtitle)
        issues = [
            *semantic_issues,
            *segment_warnings,
            *possible_chunk_boundary_duplication(transcripts),
            *structural,
        ]
        qa = _publish_qa(
            context,
            profile,
            media,
            effective_glossary,
            subtitle,
            transcripts,
            alignments,
            issues,
        )
    if qa.payload["result"] == "blocked":
        raise ExportBlockedError("structural QA remained blocked after the allowed repair")
    render, output_path = publish_srt(
        context,
        chunk_plan=media.chunk_plan,
        transcripts=transcripts,
        alignments=alignments,
        subtitle=subtitle,
        qa=qa,
    )
    context.registry.set_run_status(run_id, "succeeded")
    source_id = next(
        item.source_asset_id for item in media.probe.inputs if item.role == "source_media"
    )
    warnings = [item for item in qa.payload["issues"] if item["severity"] == "warning"]
    if media.probe.payload["timeline_status"] == "unverified":
        warnings.append({"code": "timeline_status_unverified"})
    return {
        "run_id": run_id,
        "status": "succeeded",
        "source_asset_id": source_id,
        "srt_render_artifact_id": render.artifact_id,
        "output": str(output_path.resolve()),
        "warnings": warnings,
    }


def _qa_alignment_repair_wave(
    context: ProjectContext,
    *,
    run_id: str,
    profile: str,
    runtime: RuntimeConfig,
    media_chunks: Sequence[ArtifactEnvelope],
    transcripts: Sequence[ArtifactEnvelope],
    alignments: Sequence[ArtifactEnvelope],
    workset: Sequence[str],
    aligner_factory: AlignerFactory,
) -> tuple[ArtifactEnvelope, ...]:
    chunk_by_id = {str(item.payload["chunk_id"]): item for item in media_chunks}
    transcript_by_id = {str(item.payload["chunk_id"]): item for item in transcripts}
    alignment_by_id = {str(item.payload["chunk_id"]): item for item in alignments}
    aligner = aligner_factory(runtime)
    replacements: list[ArtifactEnvelope] = []
    try:
        for chunk_id in workset:
            alignment = _alignment_with_structural_repair(
                context,
                run_id=run_id,
                profile=profile,
                runtime=runtime,
                media_chunk=chunk_by_id[chunk_id],
                transcript=transcript_by_id[chunk_id],
                aligner=aligner,
                repair_limit=0,
                operation="qa_alignment_repair",
            )
            alignment_by_id[chunk_id] = alignment
            replacements.append(alignment)
    finally:
        aligner.close()
    context.registry.activate_artifacts(
        context.project_id,
        [item.artifact_id for item in replacements],
        stale_targets=[(kind, "global") for kind in ("subtitle", "qa", "srt_render")],
    )
    return tuple(alignment_by_id[str(chunk.payload["chunk_id"])] for chunk in media_chunks)


def _structural_issues(
    context: ProjectContext,
    media: MediaBundle,
    transcripts: Sequence[ArtifactEnvelope],
    alignments: Sequence[ArtifactEnvelope],
    subtitle: ArtifactEnvelope,
) -> list[dict[str, Any]]:
    return structural_issues(
        media.media_chunks,
        transcripts,
        alignments,
        subtitle,
        duration_ms=int(media.chunk_plan.payload["duration_ms"]),
        registry=context.registry,
        project_id=context.project_id,
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
    envelope = ArtifactEnvelope.create(
        artifact_kind="subtitle",
        scope_key="global",
        producer=Producer(
            component="segmenter",
            component_version=COMPONENT_VERSION,
            processing_profile=profile,
            provider=None,
            model=None,
            config_hash=hash_json(asdict(SegmenterConfig())),
        ),
        inputs=[
            *[InputRef(role="transcript", artifact_id=item.artifact_id) for item in transcripts],
            *[InputRef(role="alignment", artifact_id=item.artifact_id) for item in alignments],
            InputRef(role="effective_glossary", artifact_id=effective_glossary.artifact_id),
        ],
        payload=payload,
    )
    context.publisher.publish(
        envelope, stale_targets=[("qa", "global"), ("srt_render", "global")]
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
    envelope = ArtifactEnvelope.create(
        artifact_kind="qa",
        scope_key="global",
        producer=Producer(
            component="qa",
            component_version=COMPONENT_VERSION,
            processing_profile=profile,
            provider=None,
            model=None,
            config_hash=hash_json(asdict(QaRulesetConfig())),
        ),
        inputs=[
            InputRef(role="chunk_plan", artifact_id=media.chunk_plan.artifact_id),
            InputRef(role="subtitle", artifact_id=subtitle.artifact_id),
            InputRef(role="effective_glossary", artifact_id=effective_glossary.artifact_id),
            *[InputRef(role="transcript", artifact_id=item.artifact_id) for item in transcripts],
            *[InputRef(role="alignment", artifact_id=item.artifact_id) for item in alignments],
        ],
        payload=qa_payload(subjects, issues),
    )
    context.publisher.publish(envelope, stale_targets=[("srt_render", "global")])
    return envelope


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
    return ArtifactEnvelope.create(
        artifact_kind="transcript",
        scope_key=str(media_chunk.payload["chunk_id"]),
        producer=Producer(
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
        ),
        inputs=[
            InputRef(role="media_chunk", artifact_id=media_chunk.artifact_id),
            InputRef(role="effective_glossary", artifact_id=effective_glossary.artifact_id),
        ],
        payload=payload,
    )


def _accepted_transcript_for_run(
    context: ProjectContext, run_id: str, chunk_id: str
) -> ArtifactEnvelope | None:
    pointer = context.registry.current_pointer(context.project_id, "transcript", chunk_id)
    if pointer is None or bool(pointer["is_stale"]):
        return None
    artifact_id = str(pointer["artifact_id"])
    if not any(
        row["operation"] == "semantic_transcription"
        and row["chunk_id"] == chunk_id
        and row["status"] == "succeeded"
        and row["artifact_id"] == artifact_id
        for row in context.registry.invocations_for_run(run_id)
    ):
        return None
    return context.artifact(artifact_id)


def _successful_semantic_attempts(
    context: ProjectContext, run_id: str, chunk_id: str, budget_window: int
) -> list[ArtifactEnvelope]:
    return [
        context.artifact(str(row["artifact_id"]))
        for row in context.registry.invocations_for_run(run_id)
        if row["operation"] == "semantic_transcription"
        and row["chunk_id"] == chunk_id
        and row["semantic_budget_window"] == budget_window
        and row["status"] == "succeeded"
        and row["artifact_id"] is not None
    ]


def _semantic_issues_for_accepted(
    context: ProjectContext,
    run_id: str,
    chunk_id: str,
    accepted: ArtifactEnvelope,
    effective_glossary: ArtifactEnvelope,
) -> list[dict[str, Any]]:
    matching = next(
        row
        for row in context.registry.invocations_for_run(run_id)
        if row["operation"] == "semantic_transcription"
        and row["artifact_id"] == accepted.artifact_id
        and row["status"] == "succeeded"
    )
    attempts = _successful_semantic_attempts(
        context, run_id, chunk_id, int(matching["semantic_budget_window"])
    )
    accepted_index = next(
        index for index, item in enumerate(attempts) if item.artifact_id == accepted.artifact_id
    )
    attempts = attempts[: accepted_index + 1]
    decision = evaluate_semantic_attempts(
        [item.payload for item in attempts],
        [str(item) for item in effective_glossary.payload.get("terms", [])],
    )
    return [
        _with_attempt_artifacts(issue, [item.artifact_id for item in attempts])
        for issue in decision.issues
    ]


def _successful_alignment_for_run(
    context: ProjectContext,
    run_id: str,
    media_chunk: ArtifactEnvelope,
    transcript: ArtifactEnvelope,
) -> ArtifactEnvelope | None:
    expected = [
        ("media_chunk", media_chunk.artifact_id),
        ("transcript", transcript.artifact_id),
    ]
    for row in reversed(context.registry.invocations_for_run(run_id)):
        if (
            row["operation"] not in {"forced_alignment", "qa_alignment_repair"}
            or row["chunk_id"] != media_chunk.scope_key
            or row["status"] != "succeeded"
            or row["artifact_id"] is None
        ):
            continue
        if _bound_inputs(context, str(row["invocation_id"])) != expected:
            continue
        alignment = context.artifact(str(row["artifact_id"]))
        if not find_unaligned_atoms(alignment.payload):
            return alignment
    return None


def _activate_transcript(context: ProjectContext, transcript: ArtifactEnvelope) -> None:
    context.registry.activate_artifacts(
        context.project_id,
        [transcript.artifact_id],
        stale_targets=[
            ("alignment", transcript.scope_key),
            ("subtitle", "global"),
            ("qa", "global"),
            ("srt_render", "global"),
        ],
    )


def _bound_inputs(context: ProjectContext, invocation_id: str) -> list[tuple[str, str]]:
    rows = context.registry.invocation_inputs(invocation_id)
    if not rows:
        raise ContractError("Invocation has no bound upstream Artifacts")
    return [(str(row["role"]), str(row["input_artifact_id"])) for row in rows]


def _retry_graph(
    context: ProjectContext, bound_inputs: Sequence[tuple[str, str]]
) -> tuple[MediaBundle, ArtifactEnvelope, ArtifactEnvelope | None]:
    by_role = {role: artifact_id for role, artifact_id in bound_inputs}
    media_chunk_id = by_role.get("media_chunk")
    if media_chunk_id is None:
        raise ContractError("retry Invocation is missing its media_chunk input")
    target_chunk = context.artifact(media_chunk_id)
    chunk_plan_id = next(
        (item.artifact_id for item in target_chunk.inputs if item.role == "chunk_plan"), None
    )
    if chunk_plan_id is None:
        raise ContractError("retry MediaChunk is missing its ChunkPlan dependency")
    chunk_plan = context.artifact(chunk_plan_id)
    timeline_id = next(
        (item.artifact_id for item in chunk_plan.inputs if item.role == "timeline_audio"), None
    )
    if timeline_id is None:
        raise ContractError("retry ChunkPlan is missing its TimelineAudio dependency")
    timeline = context.artifact(timeline_id)
    probe_id = next(
        (item.artifact_id for item in timeline.inputs if item.role == "media_probe"), None
    )
    if probe_id is None:
        raise ContractError("retry TimelineAudio is missing its MediaProbe dependency")
    probe = context.artifact(probe_id)
    chunk_rows = context.registry.dependent_artifacts(
        context.project_id, chunk_plan.artifact_id, "media_chunk"
    )
    chunks = tuple(
        sorted(
            (context.artifact(str(row["artifact_id"])) for row in chunk_rows),
            key=lambda item: int(item.payload["ordinal"]),
        )
    )
    expected_chunk_ids = [str(item["chunk_id"]) for item in chunk_plan.payload["chunks"]]
    if [str(item.payload["chunk_id"]) for item in chunks] != expected_chunk_ids:
        raise ContractError("retry media graph does not exactly cover its ChunkPlan")
    transcript = context.artifact(by_role["transcript"]) if "transcript" in by_role else None
    glossary_id = by_role.get("effective_glossary")
    if glossary_id is None and transcript is not None:
        glossary_id = next(
            (item.artifact_id for item in transcript.inputs if item.role == "effective_glossary"),
            None,
        )
    if glossary_id is None:
        raise ContractError("retry graph is missing its EffectiveGlossary dependency")
    effective = context.artifact(glossary_id)
    return MediaBundle(probe, timeline, chunk_plan, chunks), effective, transcript


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


def _default_aligner_factory(runtime: RuntimeConfig) -> ForcedAligner:
    return LocalQwenForcedAligner(runtime)


def _with_attempt_artifacts(
    issue: Mapping[str, Any], artifact_ids: Sequence[str]
) -> dict[str, Any]:
    value = dict(issue)
    value["replacement_artifact_ids"] = list(artifact_ids[1:])
    return value
