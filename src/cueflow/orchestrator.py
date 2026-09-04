from __future__ import annotations

import os
import tempfile
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, cast

from cueflow.acoustic_adjudication import adjudicate, plan_disagreement_windows
from cueflow.alignment import build_alignment_payload
from cueflow.asr_comparison import EvidenceWindow, compare_asr, extract_evidence_window
from cueflow.asr_contracts import AsrResult, ProviderMetadata, WholeFileAsrProvider
from cueflow.ata_provider import AlignmentResult, VolcengineAtaProvider
from cueflow.atomizer import build_transcript_payload
from cueflow.base_asr_provider import QwenFiletransProvider
from cueflow.canonical import hash_json
from cueflow.config import (
    COMPONENT_VERSION,
    QaRulesetConfig,
    RuntimeConfig,
    SegmenterConfig,
    result_config,
)
from cueflow.correction_provider import (
    PROMPT_VERSION,
    CorrectionProvider,
    CorrectionRequest,
    KimiCorrectionProvider,
    QwenCorrectionProvider,
    load_correction_prompt,
)
from cueflow.doubao_asr_provider import DoubaoFileAsrProvider
from cueflow.edit_resolution import (
    MATCH_POLICY,
    PROJECTION_POLICY,
    apply_resolved_payload,
    locate_edit,
    parse_edits_json,
    resolve_dual_edits,
)
from cueflow.errors import (
    ContractError,
    CueFlowError,
    DeliveryAmbiguousError,
    IntegrityError,
    ProviderError,
    ProviderUnavailableError,
)
from cueflow.export import publish_srt
from cueflow.glm_asr_provider import GlmEvidenceAsrProvider
from cueflow.job_inputs import ReferenceSpec, build_job_input_payload
from cueflow.media import MediaBundle, prepare_media, probe_source
from cueflow.media_object_store import MediaObjectStore, TosMediaObjectStore, media_ref_from_payload
from cueflow.project import ProjectContext, single_writer
from cueflow.qa import qa_payload, structural_issues
from cueflow.schema import ArtifactEnvelope, InputRef, Producer
from cueflow.segmentation import segment_subtitles

WholeAsrFactory = Callable[[], WholeFileAsrProvider]
CorrectionFactory = Callable[[], CorrectionProvider]
MediaStoreFactory = Callable[[], MediaObjectStore]
GlmFactory = Callable[[], GlmEvidenceAsrProvider]
AtaFactory = Callable[[], VolcengineAtaProvider]


@dataclass(frozen=True)
class _Factories:
    media: MediaStoreFactory = TosMediaObjectStore
    qwen: WholeAsrFactory = QwenFiletransProvider
    doubao: WholeAsrFactory = DoubaoFileAsrProvider
    glm: GlmFactory = GlmEvidenceAsrProvider
    qwen_correction: CorrectionFactory = QwenCorrectionProvider
    kimi_correction: CorrectionFactory = KimiCorrectionProvider
    ata: AtaFactory = VolcengineAtaProvider


def _config_hash() -> str:
    config = result_config()
    # Executable availability is not a semantic input to a cloud retry.
    config.pop("runtime")
    return hash_json(
        {
            **config,
            "prompt_sha256": load_correction_prompt()[1],
            "projection_policy": PROJECTION_POLICY,
            "match_policy": MATCH_POLICY,
        }
    )


def _checkpoint_args(
    context: ProjectContext,
    run_id: str,
    stage: str,
    scope: str = "global",
) -> tuple[str, str, str, str]:
    return (
        run_id,
        stage,
        scope,
        hash_json(
            {
                "run_id": run_id,
                "stage": stage,
                "scope": scope,
                "config_hash": context.registry.run(run_id)["config_hash"],
            }
        ),
    )


def _bind(context: ProjectContext, run_id: str, artifact: ArtifactEnvelope) -> ArtifactEnvelope:
    args = _checkpoint_args(context, run_id, artifact.artifact_kind, artifact.scope_key)
    context.registry.bind_checkpoint(args[0], args[1], artifact.artifact_id, args[3], args[2])
    return artifact


def _get(
    context: ProjectContext,
    run_id: str,
    stage: str,
    scope: str = "global",
) -> ArtifactEnvelope | None:
    row = context.registry.checkpoint(run_id, stage, scope)
    if row is None:
        return None
    if row["input_digest"] != _checkpoint_args(context, run_id, stage, scope)[3]:
        raise IntegrityError("checkpoint identity does not match its run/config")
    artifact = context.artifact(str(row["artifact_id"]))
    if artifact.artifact_kind != stage or artifact.scope_key != scope:
        raise IntegrityError("checkpoint kind/scope does not match its artifact")
    return artifact


def _require(context: ProjectContext, run_id: str, stage: str) -> ArtifactEnvelope:
    result = _get(context, run_id, stage)
    if result is None:
        raise IntegrityError(f"missing run checkpoint: {stage}")
    return result


def _save(
    context: ProjectContext,
    run_id: str,
    kind: str,
    payload: Mapping[str, Any],
    inputs: Sequence[ArtifactEnvelope],
    scope: str = "global",
) -> ArtifactEnvelope:
    envelope = ArtifactEnvelope.create(
        artifact_kind=kind,
        scope_key=scope,
        producer=_deterministic_producer(kind, {"run_id": run_id, "config_hash": _config_hash()}),
        inputs=[InputRef(role=item.artifact_kind, artifact_id=item.artifact_id) for item in inputs],
        payload=payload,
    )
    return context.publisher.publish(
        envelope,
        checkpoint=_checkpoint_args(context, run_id, kind, scope),
    )


def _stage(
    context: ProjectContext,
    run_id: str,
    operation: str,
    kind: str,
    action: Callable[[str | None, str | None], ArtifactEnvelope],
    retry_of: str | None,
    scope: str = "global",
) -> ArtifactEnvelope:
    complete = _get(context, run_id, kind, scope)
    if complete is not None:
        return complete
    rows = [
        row
        for row in context.registry.invocations_for_run(run_id)
        if row["logical_operation_key"] == f"{operation}:{scope}"
    ]
    latest = rows[-1] if rows else None
    if latest is not None:
        if latest["status"] == "succeeded":
            raise IntegrityError("successful invocation is missing its atomic checkpoint")
        if latest["invocation_id"] != retry_of:
            raise ProviderError(f"{operation}/{scope} previously failed; explicit retry required")
        if latest["status"] not in {
            "explicit_failure",
            "definitely_not_sent",
            "delivery_ambiguous",
        }:
            raise ContractError("only the latest failed attempt can be retried")
    result = action(
        str(latest["invocation_id"]) if latest else None,
        str(latest["idempotency_key"]) if latest else None,
    )
    return result


def _check_run(context: ProjectContext, run_id: str) -> None:
    row = context.registry.run(run_id)
    if row["project_id"] != context.project_id or row["config_hash"] != _config_hash():
        raise ContractError("run identity/config/prompt changed; create a new run")
    if row["status"] == "succeeded":
        raise ContractError("a completed run cannot be resumed")


@single_writer
def run_project(
    context: ProjectContext,
    media_path: Path,
    *,
    references: Sequence[ReferenceSpec] = (),
    keywords: Sequence[str] = (),
    runtime: RuntimeConfig | None = None,
    media_store_factory: MediaStoreFactory | None = None,
    qwen_asr_factory: WholeAsrFactory | None = None,
    doubao_asr_factory: WholeAsrFactory | None = None,
    glm_asr_factory: GlmFactory | None = None,
    qwen_correction_factory: CorrectionFactory | None = None,
    kimi_correction_factory: CorrectionFactory | None = None,
    ata_factory: AtaFactory | None = None,
) -> dict[str, Any]:
    context.registry.recover_running_source_runs()
    source = context.register_external_asset(media_path, asset_kind="media")
    job = _publish_job_input(
        context,
        source_asset_id=str(source["source_asset_id"]),
        references=references,
        keywords=keywords,
    )
    run_id = context.registry.create_source_run(
        context.project_id,
        operation_kind="run",
        source_asset_id=str(source["source_asset_id"]),
        job_input_artifact_id=job.artifact_id,
        config_hash=_config_hash(),
    )
    _bind(context, run_id, job)
    factories = _Factories(
        media_store_factory or TosMediaObjectStore,
        qwen_asr_factory or QwenFiletransProvider,
        doubao_asr_factory or DoubaoFileAsrProvider,
        glm_asr_factory or GlmEvidenceAsrProvider,
        qwen_correction_factory or QwenCorrectionProvider,
        kimi_correction_factory or KimiCorrectionProvider,
        ata_factory or VolcengineAtaProvider,
    )
    return _execute(context, run_id, factories, runtime=runtime)


@single_writer
def correct_project(
    context: ProjectContext,
    *,
    references: Sequence[ReferenceSpec] = (),
    keywords: Sequence[str] = (),
    qwen_correction_factory: CorrectionFactory | None = None,
    kimi_correction_factory: CorrectionFactory | None = None,
    glm_asr_factory: GlmFactory | None = None,
    ata_factory: AtaFactory | None = None,
    media_store_factory: MediaStoreFactory | None = None,
) -> dict[str, Any]:
    context.registry.recover_running_source_runs()
    saved = [
        context.current_artifact(kind)
        for kind in ("base_asr", "peer_asr", "media_object", "media_probe", "timeline_audio")
    ]
    base, peer, media_object = saved[:3]
    payload = build_job_input_payload(
        source_asset_id=str(base.payload["source_asset_id"]),
        references=references,
        keywords=keywords,
    )
    for arm in (base, peer):
        if arm.payload["user_keywords"] != payload["user_keywords"]:
            raise ContractError("correct cannot change UserKeywords; create a new ASR run")
        if arm.payload["source_asset_id"] != payload["source_asset_id"]:
            raise IntegrityError("Base/Peer source identity mismatch")
        if not any(item.artifact_id == media_object.artifact_id for item in arm.inputs):
            raise IntegrityError("Base/Peer media identity mismatch")
    job = _publish_payload_job_input(context, payload)
    run_id = context.registry.create_source_run(
        context.project_id,
        operation_kind="correct",
        source_asset_id=str(payload["source_asset_id"]),
        job_input_artifact_id=job.artifact_id,
        config_hash=_config_hash(),
    )
    for artifact in (job, *saved):
        _bind(context, run_id, artifact)
    factories = _Factories(
        media=media_store_factory or TosMediaObjectStore,
        glm=glm_asr_factory or GlmEvidenceAsrProvider,
        qwen_correction=qwen_correction_factory or QwenCorrectionProvider,
        kimi_correction=kimi_correction_factory or KimiCorrectionProvider,
        ata=ata_factory or VolcengineAtaProvider,
    )
    return _execute(context, run_id, factories)


@single_writer
def resume_run(
    context: ProjectContext,
    run_id: str,
    *,
    media_store_factory: MediaStoreFactory | None = None,
    qwen_asr_factory: WholeAsrFactory | None = None,
    doubao_asr_factory: WholeAsrFactory | None = None,
    glm_asr_factory: GlmFactory | None = None,
    qwen_correction_factory: CorrectionFactory | None = None,
    kimi_correction_factory: CorrectionFactory | None = None,
    ata_factory: AtaFactory | None = None,
) -> dict[str, Any]:
    context.registry.recover_running_source_runs()
    _check_run(context, run_id)
    return _execute(
        context,
        run_id,
        _Factories(
            media_store_factory or TosMediaObjectStore,
            qwen_asr_factory or QwenFiletransProvider,
            doubao_asr_factory or DoubaoFileAsrProvider,
            glm_asr_factory or GlmEvidenceAsrProvider,
            qwen_correction_factory or QwenCorrectionProvider,
            kimi_correction_factory or KimiCorrectionProvider,
            ata_factory or VolcengineAtaProvider,
        ),
    )


@single_writer
def retry_invocation(
    context: ProjectContext,
    invocation_id: str,
    *,
    media_store_factory: MediaStoreFactory | None = None,
    qwen_asr_factory: WholeAsrFactory | None = None,
    doubao_asr_factory: WholeAsrFactory | None = None,
    glm_asr_factory: GlmFactory | None = None,
    qwen_correction_factory: CorrectionFactory | None = None,
    kimi_correction_factory: CorrectionFactory | None = None,
    ata_factory: AtaFactory | None = None,
) -> dict[str, Any]:
    context.registry.recover_running_source_runs()
    row = context.registry.invocation(invocation_id)
    run_id = str(row["run_id"])
    _check_run(context, run_id)
    siblings = [
        item
        for item in context.registry.invocations_for_run(run_id)
        if item["logical_operation_key"] == row["logical_operation_key"]
    ]
    if siblings[-1]["invocation_id"] != invocation_id or row["status"] not in {
        "explicit_failure",
        "delivery_ambiguous",
        "definitely_not_sent",
    }:
        raise ContractError("only the latest terminal failed Invocation may be retried")
    final = _get(context, run_id, "edit_resolution")
    if row["operation"] == "glm_asr" and final and final.payload["sealed"]:
        raise ContractError("sealed/human-resolved decisions cannot be overwritten by GLM retry")
    return _execute(
        context,
        run_id,
        _Factories(
            media_store_factory or TosMediaObjectStore,
            qwen_asr_factory or QwenFiletransProvider,
            doubao_asr_factory or DoubaoFileAsrProvider,
            glm_asr_factory or GlmEvidenceAsrProvider,
            qwen_correction_factory or QwenCorrectionProvider,
            kimi_correction_factory or KimiCorrectionProvider,
            ata_factory or VolcengineAtaProvider,
        ),
        retry_of=invocation_id,
    )


def _execute(
    context: ProjectContext,
    run_id: str,
    factories: _Factories,
    *,
    runtime: RuntimeConfig | None = None,
    retry_of: str | None = None,
) -> dict[str, Any]:
    try:
        context.registry.set_run_status(run_id, "running")
        job = _get(context, run_id, "job_input")
        if job is None:
            job = context.artifact(str(context.registry.run(run_id)["job_input_artifact_id"]))
            _bind(context, run_id, job)
        timeline = _get(context, run_id, "timeline_audio")
        if timeline is None:
            source = context.registry.source_asset(
                context.project_id, str(job.payload["source_asset_id"])
            )
            source_path = context.verify_external_asset(str(source["source_asset_id"]))
            chosen_runtime = runtime or RuntimeConfig.detect()
            probe = probe_source(source_path, chosen_runtime)
            media = prepare_media(context, dict(source), probe, chosen_runtime)
            _bind(context, run_id, media.probe)
            _bind(context, run_id, media.timeline_audio)
        else:
            media = MediaBundle(_require(context, run_id, "media_probe"), timeline)
        media_object = _stage(
            context,
            run_id,
            "media_upload",
            "media_object",
            lambda retry, key: _upload_for_run(context, run_id, job, factories.media, retry, key),
            retry_of,
        )
        # Sign only when a URL-consuming Provider is actually invoked. GLM and
        # checkpoint-only resumes must not depend on TOS credentials/availability.
        media_url: str | None = None

        def get_media_url() -> str:
            nonlocal media_url
            if media_url is None:
                media_url = _presign(context, media_object, factories.media)
            return media_url

        keywords = tuple(cast(Sequence[str], job.payload["user_keywords"]))
        base = _stage(
            context,
            run_id,
            "qwen_asr",
            "base_asr",
            lambda retry, key: _whole_asr(
                context,
                run_id,
                "base_asr",
                "qwen_asr",
                media_object,
                job,
                get_media_url(),
                keywords,
                factories.qwen(),
                retry_of=retry,
                idempotency_key=key,
            ),
            retry_of,
        )
        peer = _stage(
            context,
            run_id,
            "doubao_asr",
            "peer_asr",
            lambda retry, key: _whole_asr(
                context,
                run_id,
                "peer_asr",
                "doubao_asr",
                media_object,
                job,
                get_media_url(),
                keywords,
                factories.doubao(),
                retry_of=retry,
                idempotency_key=key,
            ),
            retry_of,
        )
        comparison = _get(context, run_id, "asr_comparison")
        if comparison is None:
            comparison = _bind(context, run_id, _comparison(context, base, peer))
        request = CorrectionRequest(
            str(base.payload["source_text"]),
            str(peer.payload["source_text"]),
            tuple(job.payload["references"]),
            keywords,
            tuple(comparison.payload["hunks"]),
        )
        proposals = []
        for arm, factory in (
            ("qwen", factories.qwen_correction),
            ("kimi", factories.kimi_correction),
        ):

            def call_correction(
                retry: str | None, key: str | None, factory: CorrectionFactory = factory
            ) -> ArtifactEnvelope:
                return _correction_arm(
                    context,
                    run_id,
                    job,
                    base,
                    peer,
                    comparison,
                    request,
                    factory(),
                    retry_of=retry,
                    idempotency_key=key,
                )

            artifact = _stage(
                context,
                run_id,
                f"{arm}_correction",
                f"{arm}_edit_proposal",
                call_correction,
                retry_of,
            )
            proposals.append(artifact)
        agreement = _get(context, run_id, "agreement_resolution")
        if agreement is None:
            payload = resolve_dual_edits(
                request.base_text,
                parse_edits_json({"edits": proposals[0].payload["edits"]}),
                parse_edits_json({"edits": proposals[1].payload["edits"]}),
            )
            agreement = _save(context, run_id, "agreement_resolution", payload, [base, *proposals])
        final = _get(context, run_id, "edit_resolution")
        if final is None or not final.payload["sealed"]:
            final = _post_correction_adjudication_stage(
                context,
                run_id,
                media,
                base,
                job,
                agreement,
                factories.glm,
                retry_of,
            )
        if not final.payload["sealed"]:
            context.registry.set_run_status(run_id, "needs_review")
            queue = _require(context, run_id, "review_queue")
            return {
                "status": "needs_review",
                "run_id": run_id,
                "review_queue_artifact_id": queue.artifact_id,
                "review_item_count": len(queue.payload["items"]),
            }
        transcript = _get(context, run_id, "transcript")
        if transcript is None:
            transcript = _save(
                context,
                run_id,
                "transcript",
                build_transcript_payload(
                    source_text=str(final.payload["corrected_preview"]),
                    base_asr_artifact_id=base.artifact_id,
                    edit_resolution_artifact_id=final.artifact_id,
                    correction_mode="post_correction_adjudication",
                ),
                [base, final],
            )
        alignment = _stage(
            context,
            run_id,
            "ata",
            "alignment",
            lambda retry, key: _ata_stage(
                context,
                run_id,
                media,
                media_object,
                transcript,
                get_media_url(),
                factories.ata(),
                retry_of=retry,
                idempotency_key=key,
            ),
            retry_of,
        )
        context.registry.activate_artifacts(
            context.project_id,
            [
                item.artifact_id
                for item in (
                    job,
                    media.probe,
                    media.timeline_audio,
                    media_object,
                    base,
                    peer,
                    comparison,
                    *proposals,
                    agreement,
                    final,
                    transcript,
                    alignment,
                )
            ],
        )
        result = _publish_downstream(context, run_id, media, transcript, alignment)
        context.registry.set_run_status(run_id, "succeeded")
        return result
    except BaseException as exc:
        _fail_run(context, run_id, exc)
        raise


def _upload_for_run(
    context: ProjectContext,
    run_id: str,
    job: ArtifactEnvelope,
    factory: MediaStoreFactory,
    retry: str | None,
    key: str | None,
) -> ArtifactEnvelope:
    store = factory()
    try:
        return _upload_media(
            context,
            run_id,
            context.verify_external_asset(str(job.payload["source_asset_id"])),
            job,
            store,
            retry_of=retry,
            idempotency_key=key,
        )
    finally:
        store.close()


def _correction_arm(
    context: ProjectContext,
    run_id: str,
    job: ArtifactEnvelope,
    base: ArtifactEnvelope,
    peer: ArtifactEnvelope,
    comparison: ArtifactEnvelope,
    request: CorrectionRequest,
    provider: CorrectionProvider,
    *,
    retry_of: str | None = None,
    idempotency_key: str | None = None,
) -> ArtifactEnvelope:
    inputs = [base, peer, job, comparison]
    invocation = _new_invocation(
        context,
        run_id,
        f"{provider.arm}_correction",
        provider.provider,
        provider.model,
        [(item.artifact_kind, item.artifact_id) for item in inputs],
        prompt_version=PROMPT_VERSION,
        prompt_sha256=load_correction_prompt()[1],
        retry_of=retry_of,
        idempotency_key=idempotency_key,
    )
    try:
        result = provider.correct(request)
        envelope = ArtifactEnvelope.create(
            artifact_kind=f"{provider.arm}_edit_proposal",
            scope_key="global",
            producer=_provider_producer(
                provider.provider,
                provider.model,
                {
                    "prompt_version": PROMPT_VERSION,
                    "prompt_sha256": load_correction_prompt()[1],
                    "live_search_replayable": False,
                },
            ),
            inputs=[
                InputRef(role=item.artifact_kind, artifact_id=item.artifact_id) for item in inputs
            ],
            payload={
                "edits": [item.as_dict() for item in result.edits],
                "provider_metadata": result.metadata.as_dict(),
            },
        )
        _succeed_with_metadata(context, invocation, envelope, result.metadata)
        return envelope
    except BaseException as exc:
        _record_invocation_failure(context, invocation, exc)
        raise
    finally:
        provider.close()


def _post_correction_adjudication_stage(
    context: ProjectContext,
    run_id: str,
    media: MediaBundle,
    base: ArtifactEnvelope,
    job: ArtifactEnvelope,
    agreement: ArtifactEnvelope,
    factory: GlmFactory,
    retry_of: str | None,
) -> ArtifactEnvelope:
    disputes = cast(list[dict[str, Any]], agreement.payload["lexical_disagreements"])
    plan = _get(context, run_id, "acoustic_window_plan")
    if plan is None:
        planned = plan_disagreement_windows(
            disputes,
            str(base.payload["source_text"]),
            _units_from_payload(base.payload),
            int(media.timeline_audio.payload["duration_ms"]),
        )
        plan = _save(
            context,
            run_id,
            "acoustic_window_plan",
            {**planned, "run_id": run_id},
            [agreement, base, media.timeline_audio],
        )
    by_id = {str(item["disagreement_id"]): item for item in disputes}
    outcomes: dict[str, ArtifactEnvelope] = {}
    for raw in plan.payload["unavailable"]:
        identity = str(raw["disagreement_id"])
        outcomes[identity] = _get(context, run_id, "acoustic_resolution", identity) or _save(
            context,
            run_id,
            "acoustic_resolution",
            {
                **raw,
                "status": "review",
                "evidence_artifact_id": None,
            },
            [plan],
            identity,
        )
    for raw in plan.payload["windows"]:
        identity = str(raw["window_id"])
        retry_window = (
            retry_of is not None
            and context.registry.invocation(retry_of)["logical_operation_key"]
            == f"glm_asr:{identity}"
        )
        cached = {
            dispute_id: _get(context, run_id, "acoustic_resolution", dispute_id)
            for dispute_id in raw["disagreement_ids"]
        }
        if not retry_window and all(value is not None for value in cached.values()):
            outcomes.update({key: value for key, value in cached.items() if value is not None})
            continue
        # Extraction/I/O integrity failures must not be caught as Provider failures.
        window = _get(context, run_id, "acoustic_window", identity)
        if window is None:
            try:
                window = _extract_window(context, run_id, media.timeline_audio, plan, raw)
            except ContractError:
                for dispute_id in raw["disagreement_ids"]:
                    outcomes[dispute_id] = _save(
                        context,
                        run_id,
                        "acoustic_resolution",
                        {
                            "disagreement_id": dispute_id,
                            "status": "review",
                            "reason": "WINDOW_EXTRACTION_LIMIT",
                            "evidence_artifact_id": None,
                        },
                        [plan],
                        dispute_id,
                    )
                continue
        evidence: ArtifactEnvelope | None = None
        assert window is not None
        frozen_window = window

        def call_glm(
            retry: str | None,
            key: str | None,
            window: ArtifactEnvelope = frozen_window,
        ) -> ArtifactEnvelope:
            return _glm_window(
                context, run_id, window, job, factory(), retry_of=retry, idempotency_key=key
            )

        try:
            evidence = _stage(
                context,
                run_id,
                "glm_asr",
                "glm_adjudication_evidence",
                call_glm,
                retry_of,
                identity,
            )
        except (ProviderError, ContractError, TimeoutError) as exc:
            # Schema/registry/artifact corruption is IntegrityError, not a
            # local acoustic failure. Unexpected programming errors still escape.
            failure = type(exc).__name__
        for dispute_id in raw["disagreement_ids"]:
            dispute = by_id[dispute_id]
            if evidence is None:
                result = {
                    "disagreement_id": dispute_id,
                    "status": "review",
                    "reason": "GLM_UNAVAILABLE",
                    "failure_type": failure,
                    "evidence_artifact_id": None,
                }
            else:
                result = adjudicate(
                    str(base.payload["source_text"]), dispute, str(evidence.payload["source_text"])
                )
                result["evidence_artifact_id"] = evidence.artifact_id
            outcomes[dispute_id] = _save(
                context,
                run_id,
                "acoustic_resolution",
                result,
                [agreement, window, *([evidence] if evidence else [])],
                dispute_id,
            )
    if set(outcomes) != set(by_id):
        raise IntegrityError("acoustic plan/outcomes do not cover every disagreement")
    accepted = list(agreement.payload["resolved_edits"])
    reviews = list(agreement.payload["review_items"])
    for identity, outcome in outcomes.items():
        dispute, choice = by_id[identity], outcome.payload
        if choice["status"] == "review":
            reviews.append(
                {
                    **dispute,
                    "review_id": identity,
                    "reason": choice["reason"],
                    "acoustic_resolution_artifact_id": outcome.artifact_id,
                }
            )
        elif choice["action"] == "replace":
            accepted.append(
                {
                    "start": dispute["start"],
                    "end": dispute["end"],
                    "original": dispute["original"],
                    "replacement": choice["selected_text"],
                    "resolution": "glm_unique_candidate",
                    "acoustic_resolution_artifact_id": outcome.artifact_id,
                }
            )
    return _finalize_resolution_stage(
        context,
        run_id,
        base,
        agreement,
        accepted,
        reviews,
        [plan, *outcomes.values()],
    )


def _extract_window(
    context: ProjectContext,
    run_id: str,
    timeline: ArtifactEnvelope,
    plan: ArtifactEnvelope,
    raw: Mapping[str, Any],
) -> ArtifactEnvelope:
    blob = timeline.payload["audio_blob"]
    timeline_path = context.store.blob_path(str(blob["content_hash"]))
    context.store.verify_blob(timeline_path, str(blob["content_hash"]), int(blob["byte_length"]))
    window = EvidenceWindow(
        str(raw["window_id"]),
        int(raw["global_start_ms"]),
        int(raw["global_end_ms"]),
        tuple(raw["disagreement_ids"]),
    )
    fd, path = tempfile.mkstemp(prefix="acoustic-", suffix=".wav", dir=context.store.temp_root)
    os.close(fd)
    try:
        size = extract_evidence_window(timeline_path, Path(path), window)
        digest, _, _ = context.store.publish_blob(Path(path))
        return _save(
            context,
            run_id,
            "acoustic_window",
            {
                **raw,
                "audio_blob": {
                    "content_hash": digest,
                    "byte_length": size,
                    "media_type": "audio/wav",
                },
            },
            [timeline, plan],
            window.window_id,
        )
    finally:
        Path(path).unlink(missing_ok=True)


def _glm_window(
    context: ProjectContext,
    run_id: str,
    window: ArtifactEnvelope,
    job: ArtifactEnvelope,
    provider: GlmEvidenceAsrProvider,
    *,
    retry_of: str | None = None,
    idempotency_key: str | None = None,
) -> ArtifactEnvelope:
    blob = window.payload["audio_blob"]
    path = context.store.blob_path(str(blob["content_hash"]))
    context.store.verify_blob(path, str(blob["content_hash"]), int(blob["byte_length"]))
    invocation = _new_invocation(
        context,
        run_id,
        "glm_asr",
        provider.provider,
        provider.model,
        [("acoustic_window", window.artifact_id), ("job_input", job.artifact_id)],
        logical_suffix=window.scope_key,
        retry_of=retry_of,
        idempotency_key=idempotency_key,
    )
    try:
        keywords = tuple(job.payload["user_keywords"])
        result = provider.transcribe(path, user_keywords=keywords)
        evidence = ArtifactEnvelope.create(
            artifact_kind="glm_adjudication_evidence",
            scope_key=window.scope_key,
            producer=_provider_producer(provider.provider, provider.model, {"prompt": "absent"}),
            inputs=[
                InputRef(role="acoustic_window", artifact_id=window.artifact_id),
                InputRef(role="job_input", artifact_id=job.artifact_id),
            ],
            payload={
                **_asr_payload(result, str(job.payload["source_asset_id"]), keywords),
                "window_id": window.scope_key,
            },
        )
        _succeed_with_metadata(context, invocation, evidence, result.metadata)
        return evidence
    except BaseException as exc:
        _record_invocation_failure(context, invocation, exc)
        raise
    finally:
        provider.close()


def _finalize_resolution_stage(
    context: ProjectContext,
    run_id: str,
    base: ArtifactEnvelope,
    agreement: ArtifactEnvelope,
    edits: list[dict[str, Any]],
    reviews: list[dict[str, Any]],
    evidence: Sequence[ArtifactEnvelope],
) -> ArtifactEnvelope:
    with context.registry.transaction():
        final = _save(
            context,
            run_id,
            "edit_resolution",
            {
                "run_id": run_id,
                "base_artifact_id": base.artifact_id,
                "base_text": base.payload["source_text"],
                "resolved_edits": edits,
                "review_items": reviews,
                "pending_acoustic": 0,
                "sealed": not reviews,
                "corrected_preview": apply_resolved_payload(
                    str(base.payload["source_text"]), edits
                ),
            },
            [base, agreement, *evidence],
        )
        _save(
            context,
            run_id,
            "review_queue",
            {
                "run_id": run_id,
                "status": "needs_review" if reviews else "clear",
                "items": reviews,
                "resolution_artifact_id": final.artifact_id,
            },
            [final],
        )
    return final


@single_writer
def resolve_review(
    context: ProjectContext,
    decisions: Sequence[Mapping[str, Any]],
    *,
    run_id: str,
    expected_review_queue_artifact_id: str,
    ata_factory: AtaFactory | None = None,
    media_store_factory: MediaStoreFactory | None = None,
) -> dict[str, Any]:
    context.registry.recover_running_source_runs()
    _check_run(context, run_id)
    queue = _require(context, run_id, "review_queue")
    if (
        queue.artifact_id != expected_review_queue_artifact_id
        or queue.payload["status"] != "needs_review"
    ):
        raise ContractError("stale or already resolved review queue")
    final = _require(context, run_id, "edit_resolution")
    if final.artifact_id != queue.payload["resolution_artifact_id"] or final.payload["sealed"]:
        raise ContractError("review queue no longer binds the active resolution")
    for decision in decisions:
        if not isinstance(decision, Mapping):
            raise ContractError("review decision must be an object")
        fields = {"review_id", "action"}
        if decision.get("action") == "replace":
            fields.add("edit")
        if set(decision) != fields:
            raise ContractError("review decision fields do not match the contract")
    items = {str(item["review_id"]): item for item in queue.payload["items"]}
    ids = [str(item.get("review_id", "")) for item in decisions]
    if len(ids) != len(set(ids)) or set(ids) != set(items):
        raise ContractError("decisions must cover stable review IDs exactly once")
    base = _require(context, run_id, "base_asr")
    base_text = str(base.payload["source_text"])
    manual: list[dict[str, Any]] = []
    for decision in decisions:
        identity = str(decision["review_id"])
        item = items[identity]
        action = decision.get("action")
        if action == "keep":
            continue
        if action in {"qwen", "kimi"}:
            if "candidates" not in item:
                raise ContractError(
                    "unlocated/contradictory edit requires keep or an exact manual edit"
                )
            start, end = int(item["start"]), int(item["end"])
            replacement = str(item["candidates"][action])
        elif action == "replace":
            edits = parse_edits_json({"edits": [decision.get("edit")]})
            found = locate_edit(base_text, edits[0])
            start, end, replacement = found.start, found.end, found.replacement
        else:
            raise ContractError("review action must be keep/qwen/kimi/replace")
        manual.append(
            {
                "start": start,
                "end": end,
                "original": base_text[start:end],
                "replacement": replacement,
                "resolution": "human",
                "review_id": identity,
            }
        )
    combined = [*final.payload["resolved_edits"], *manual]
    apply_resolved_payload(base_text, combined)
    with context.registry.transaction():
        review = _save(
            context,
            run_id,
            "review_resolution",
            {
                "run_id": run_id,
                "queue_artifact_id": queue.artifact_id,
                "decisions": [dict(item) for item in decisions],
            },
            [queue, base],
        )
        _finalize_resolution_stage(
            context,
            run_id,
            base,
            _require(context, run_id, "agreement_resolution"),
            combined,
            [],
            [final, review],
        )
    return _execute(
        context,
        run_id,
        _Factories(
            media=media_store_factory or TosMediaObjectStore,
            ata=ata_factory or VolcengineAtaProvider,
        ),
    )


def initialize_project(root: Path, display_name: str) -> ProjectContext:
    return ProjectContext.create(root, display_name)


def project_status(context: ProjectContext) -> dict[str, Any]:
    project = context.registry.project()
    runs = context.registry.runs(context.project_id)
    latest = runs[-1] if runs else None
    return {
        "project_id": context.project_id,
        "display_name": str(project["display_name"]),
        "latest_run": dict(latest) if latest is not None else None,
        "current_artifacts": [
            dict(row) for row in context.registry.current_artifacts(context.project_id)
        ],
    }


def _presign(
    context: ProjectContext,
    media_object: ArtifactEnvelope,
    store_factory: MediaStoreFactory,
) -> str:
    del context
    store = store_factory()
    try:
        return store.presign_get(media_ref_from_payload(dict(media_object.payload)))
    finally:
        store.close()


def _bound_invocation_inputs(
    context: ProjectContext, invocation_id: str
) -> dict[str, list[ArtifactEnvelope]]:
    result: dict[str, list[ArtifactEnvelope]] = {}
    for row in context.registry.invocation_inputs(invocation_id):
        result.setdefault(str(row["role"]), []).append(
            context.artifact(str(row["input_artifact_id"]))
        )
    return result


def _one_bound(bound: Mapping[str, Sequence[ArtifactEnvelope]], role: str) -> ArtifactEnvelope:
    values = bound.get(role, ())
    if len(values) != 1:
        raise ContractError(f"retry requires exactly one bound {role} Artifact")
    return values[0]


def _publish_job_input(
    context: ProjectContext,
    *,
    source_asset_id: str,
    references: Sequence[ReferenceSpec],
    keywords: Sequence[str],
) -> ArtifactEnvelope:
    return _publish_payload_job_input(
        context,
        build_job_input_payload(
            source_asset_id=source_asset_id, references=references, keywords=keywords
        ),
    )


def _publish_payload_job_input(
    context: ProjectContext, payload: Mapping[str, Any]
) -> ArtifactEnvelope:
    envelope = ArtifactEnvelope.create(
        artifact_kind="job_input",
        scope_key="global",
        producer=_deterministic_producer("job_input", {"format": "0.5.2"}),
        inputs=[InputRef(role="source_media", source_asset_id=str(payload["source_asset_id"]))],
        payload=payload,
    )
    return context.publisher.publish(
        envelope,
        stale_targets=[
            ("qwen_edit_proposal", None),
            ("kimi_edit_proposal", None),
            ("edit_proposal", None),
            ("agreement_resolution", None),
            ("acoustic_window_plan", None),
            ("acoustic_window", None),
            ("glm_adjudication_evidence", None),
            ("acoustic_resolution", None),
            ("review_resolution", None),
            ("edit_resolution", None),
            ("review_queue", None),
            ("transcript", None),
            ("alignment", None),
            ("subtitle", None),
            ("qa", None),
            ("srt_render", None),
        ],
    )


def _upload_media(
    context: ProjectContext,
    run_id: str,
    path: Path,
    job_input: ArtifactEnvelope,
    store: MediaObjectStore,
    *,
    retry_of: str | None = None,
    idempotency_key: str | None = None,
) -> ArtifactEnvelope:
    invocation = _new_invocation(
        context,
        run_id,
        "media_upload",
        store.provider,
        None,
        [("job_input", job_input.artifact_id)],
        retry_of=retry_of,
        idempotency_key=idempotency_key,
    )
    try:
        ref = store.upload(path)
        envelope = ArtifactEnvelope.create(
            artifact_kind="media_object",
            scope_key="global",
            producer=_provider_producer(store.provider, None, {"url_persisted": False}),
            inputs=[
                InputRef(
                    role="source_media", source_asset_id=str(job_input.payload["source_asset_id"])
                )
            ],
            payload=ref.artifact_payload(str(job_input.payload["source_asset_id"])),
        )
        _succeed_with_metadata(
            context,
            invocation,
            envelope,
            ProviderMetadata(store.provider, "media-upload"),
            stale_targets=[
                ("base_asr", None),
                ("peer_asr", None),
                ("asr_comparison", None),
                ("alignment", None),
                ("subtitle", None),
                ("qa", None),
                ("srt_render", None),
            ],
        )
        return envelope
    except BaseException as exc:
        _record_invocation_failure(context, invocation, exc)
        raise


def _whole_asr(
    context: ProjectContext,
    run_id: str,
    artifact_kind: str,
    operation: str,
    media_object: ArtifactEnvelope,
    job_input: ArtifactEnvelope,
    media_url: str,
    keywords: Sequence[str],
    provider: WholeFileAsrProvider,
    *,
    retry_of: str | None = None,
    idempotency_key: str | None = None,
) -> ArtifactEnvelope:
    invocation = _new_invocation(
        context,
        run_id,
        operation,
        provider.provider,
        provider.model,
        [("media_object", media_object.artifact_id), ("job_input", job_input.artifact_id)],
        retry_of=retry_of,
        idempotency_key=idempotency_key,
    )
    try:
        result = provider.transcribe(media_url, user_keywords=keywords)
        payload = _asr_payload(result, str(job_input.payload["source_asset_id"]), keywords)
        envelope = ArtifactEnvelope.create(
            artifact_kind=artifact_kind,
            scope_key="global",
            producer=_provider_producer(provider.provider, provider.model, {"whole_file": True}),
            inputs=[
                InputRef(role="media_object", artifact_id=media_object.artifact_id),
                InputRef(role="job_input", artifact_id=job_input.artifact_id),
            ],
            payload=payload,
        )
        _succeed_with_metadata(
            context,
            invocation,
            envelope,
            result.metadata,
            stale_targets=[
                ("asr_comparison", None),
                ("qwen_edit_proposal", None),
                ("kimi_edit_proposal", None),
                ("edit_proposal", None),
                ("edit_resolution", None),
                ("review_queue", None),
                ("transcript", None),
                ("alignment", None),
                ("subtitle", None),
                ("qa", None),
                ("srt_render", None),
            ],
        )
        return envelope
    except BaseException as exc:
        _record_invocation_failure(context, invocation, exc)
        raise
    finally:
        provider.close()


def _comparison(
    context: ProjectContext, base: ArtifactEnvelope, peer: ArtifactEnvelope
) -> ArtifactEnvelope:
    hunks = compare_asr(
        str(base.payload["source_text"]),
        str(peer.payload["source_text"]),
        _units_from_payload(base.payload),
        _units_from_payload(peer.payload),
    )
    envelope = ArtifactEnvelope.create(
        artifact_kind="asr_comparison",
        scope_key="global",
        producer=_deterministic_producer(
            "character_diff", {"algorithm": "difflib-sequence-matcher-v1", "normalization": "none"}
        ),
        inputs=[
            InputRef(role="base_asr", artifact_id=base.artifact_id),
            InputRef(role="peer_asr", artifact_id=peer.artifact_id),
        ],
        payload={"hunks": hunks},
    )
    return context.publisher.publish(
        envelope,
        stale_targets=[
            ("acoustic_window", None),
            ("glm_adjudication_evidence", None),
            ("qwen_edit_proposal", None),
            ("kimi_edit_proposal", None),
            ("edit_proposal", None),
            ("edit_resolution", None),
            ("review_queue", None),
            ("transcript", None),
            ("alignment", None),
            ("subtitle", None),
            ("qa", None),
            ("srt_render", None),
        ],
    )


def _ata_stage(
    context: ProjectContext,
    run_id: str,
    media: MediaBundle,
    media_object: ArtifactEnvelope,
    transcript: ArtifactEnvelope,
    media_url: str,
    provider: VolcengineAtaProvider,
    *,
    retry_of: str | None = None,
    idempotency_key: str | None = None,
) -> ArtifactEnvelope:
    invocation = _new_invocation(
        context,
        run_id,
        "ata",
        provider.provider,
        provider.model,
        [("media_object", media_object.artifact_id), ("transcript", transcript.artifact_id)],
        retry_of=retry_of,
        idempotency_key=idempotency_key,
    )
    try:
        result: AlignmentResult = provider.align(media_url, str(transcript.payload["source_text"]))
        payload = build_alignment_payload(
            media_object_artifact_id=media_object.artifact_id,
            timeline_audio_artifact_id=media.timeline_audio.artifact_id,
            duration_ms=int(media.timeline_audio.payload["duration_ms"]),
            transcript_artifact_id=transcript.artifact_id,
            transcript=transcript.payload,
            tokens=result.tokens,
        )
        envelope = ArtifactEnvelope.create(
            artifact_kind="alignment",
            scope_key="global",
            producer=_provider_producer(
                provider.provider, provider.model, {"sta_punc_mode": 3, "transport": "url"}
            ),
            inputs=[
                InputRef(role="media_object", artifact_id=media_object.artifact_id),
                InputRef(role="timeline_audio", artifact_id=media.timeline_audio.artifact_id),
                InputRef(role="transcript", artifact_id=transcript.artifact_id),
            ],
            payload=payload,
        )
        _succeed_with_metadata(
            context,
            invocation,
            envelope,
            result.metadata,
            stale_targets=[("subtitle", None), ("qa", None), ("srt_render", None)],
        )
        return envelope
    except BaseException as exc:
        _record_invocation_failure(context, invocation, exc)
        raise
    finally:
        provider.close()


def _publish_downstream(
    context: ProjectContext,
    run_id: str,
    media: MediaBundle,
    transcript: ArtifactEnvelope,
    alignment: ArtifactEnvelope,
) -> dict[str, Any]:
    duration_ms = int(media.timeline_audio.payload["duration_ms"])
    subtitle = ArtifactEnvelope.create(
        artifact_kind="subtitle",
        scope_key="global",
        producer=_deterministic_producer("segmenter", asdict(SegmenterConfig())),
        inputs=[
            InputRef(role="transcript", artifact_id=transcript.artifact_id),
            InputRef(role="alignment", artifact_id=alignment.artifact_id),
        ],
        payload=segment_subtitles(
            transcript, alignment, duration_ms=duration_ms, config=SegmenterConfig()
        ),
    )
    context.publisher.publish(subtitle, stale_targets=[("qa", None), ("srt_render", None)])
    subjects = [transcript.artifact_id, alignment.artifact_id, subtitle.artifact_id]
    qa = ArtifactEnvelope.create(
        artifact_kind="qa",
        scope_key="global",
        producer=_deterministic_producer("qa", asdict(QaRulesetConfig())),
        inputs=[
            InputRef(role="transcript", artifact_id=transcript.artifact_id),
            InputRef(role="alignment", artifact_id=alignment.artifact_id),
            InputRef(role="subtitle", artifact_id=subtitle.artifact_id),
        ],
        payload=qa_payload(
            subjects, structural_issues(transcript, alignment, subtitle, duration_ms=duration_ms)
        ),
    )
    context.publisher.publish(qa, stale_targets=[("srt_render", None)])
    render, output = publish_srt(
        context,
        timeline_audio=media.timeline_audio,
        transcript=transcript,
        alignment=alignment,
        subtitle=subtitle,
        qa=qa,
    )
    return {
        "status": "succeeded",
        "run_id": run_id,
        "base_asr_artifact_id": transcript.payload["base_asr_artifact_id"],
        "transcript_artifact_id": transcript.artifact_id,
        "alignment_artifact_id": alignment.artifact_id,
        "subtitle_artifact_id": subtitle.artifact_id,
        "qa_artifact_id": qa.artifact_id,
        "srt_render_artifact_id": render.artifact_id,
        "output_path": str(output.resolve()),
    }


def _new_invocation(
    context: ProjectContext,
    run_id: str,
    operation: str,
    provider: str,
    requested_model: str | None,
    inputs: Sequence[tuple[str, str]],
    *,
    logical_suffix: str = "global",
    prompt_version: str | None = None,
    prompt_sha256: str | None = None,
    retry_of: str | None = None,
    idempotency_key: str | None = None,
) -> str:
    if retry_of:
        original = context.registry.invocation(retry_of)
        original_inputs = [
            (str(row["role"]), str(row["input_artifact_id"]))
            for row in context.registry.invocation_inputs(retry_of)
        ]
        if (
            original["run_id"] != run_id
            or original["provider"] != provider
            or original["requested_model"] != requested_model
            or list(inputs) != original_inputs
            or original["prompt_version"] != prompt_version
            or original["prompt_sha256"] != prompt_sha256
        ):
            raise IntegrityError("targeted retry changed original request identity")
    invocation = context.registry.create_invocation(
        run_id=run_id,
        project_id=context.project_id,
        operation=operation,
        logical_operation_key=f"{operation}:{logical_suffix}",
        provider=provider,
        requested_model=requested_model,
        idempotency_key=idempotency_key or str(uuid.uuid4()),
        inputs=inputs,
        prompt_version=prompt_version,
        prompt_sha256=prompt_sha256,
        retry_of_invocation_id=retry_of,
    )
    context.registry.set_invocation_status(invocation, "sending")
    return invocation


def _record_invocation_failure(
    context: ProjectContext, invocation: str, exc: BaseException
) -> None:
    if context.registry.invocation(invocation)["status"] == "succeeded":
        return
    if isinstance(exc, DeliveryAmbiguousError):
        status = "delivery_ambiguous"
    elif isinstance(exc, ProviderUnavailableError):
        status = "definitely_not_sent"
    elif isinstance(exc, (ProviderError, ContractError)):
        status = "explicit_failure"
    else:
        status = "delivery_ambiguous"
    context.registry.set_invocation_status(invocation, status, error_message=str(exc))


def _succeed_with_metadata(
    context: ProjectContext,
    invocation: str,
    envelope: ArtifactEnvelope | None,
    metadata: ProviderMetadata,
    *,
    stale_targets: Sequence[tuple[str, str | None]] = (),
) -> None:
    if envelope is None:
        raise IntegrityError("successful invocation requires its result artifact")
    run_id = str(context.registry.invocation(invocation)["run_id"])
    context.publisher.publish(
        envelope,
        stale_targets=stale_targets,
        checkpoint=_checkpoint_args(context, run_id, envelope.artifact_kind, envelope.scope_key),
        invocation_id=invocation,
        metadata=metadata.as_dict(),
    )


def _asr_payload(
    result: AsrResult, source_asset_id: str, keywords: Sequence[str]
) -> dict[str, Any]:
    return {
        "source_asset_id": source_asset_id,
        "source_text": result.source_text,
        "timed_units": [unit.as_dict() for unit in result.timed_units],
        "provider_metadata": result.metadata.as_dict(),
        "user_keywords": list(keywords),
    }


def _units_from_payload(payload: Mapping[str, Any]) -> tuple[Any, ...]:
    from cueflow.asr_contracts import TimedUnit

    result: list[TimedUnit] = []
    for raw in cast(Sequence[Mapping[str, Any]], payload["timed_units"]):
        result.append(
            TimedUnit(
                str(raw["text"]),
                int(raw["start_ms"]),
                int(raw["end_ms"]),
                cast(Mapping[str, Any] | None, raw.get("confidence")),
            )
        )
    return tuple(result)


def _deterministic_producer(component: str, config: Mapping[str, Any]) -> Producer:
    return Producer(component, COMPONENT_VERSION, None, None, hash_json(config))


def _provider_producer(provider: str, model: str | None, config: Mapping[str, Any]) -> Producer:
    return Producer(provider, COMPONENT_VERSION, provider, model, hash_json(config))


def _fail_run(context: ProjectContext, run_id: str, exc: BaseException) -> None:
    try:
        row = context.registry.run(run_id)
        if row["status"] not in {"succeeded", "needs_review"}:
            context.registry.set_run_status(
                run_id,
                "interrupted" if isinstance(exc, KeyboardInterrupt) else "failed",
                error_message=str(exc) or type(exc).__name__,
            )
    except CueFlowError:
        return
