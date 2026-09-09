from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from cueflow.asr_comparison import compare_asr
from cueflow.asr_contracts import AsrResult, ProviderMetadata, WholeFileAsrProvider
from cueflow.ata_provider import AtaResponse, VolcengineAtaProvider
from cueflow.ata_result import ata_diagnostics, parse_ata_result
from cueflow.base_asr_provider import QwenFiletransProvider
from cueflow.canonical import hash_json
from cueflow.cloud_stream import CompletedResponseError
from cueflow.config import (
    COMPONENT_VERSION,
    RuntimeConfig,
    result_config,
)
from cueflow.conflict_selection import (
    MERGE_POLICY,
    apply_selections,
    build_merge_plan,
    build_selection_batches,
)
from cueflow.correction_provider import (
    PROMPT_VERSION,
    CorrectionProvider,
    CorrectionRequest,
    CorrectionResult,
    KimiCorrectionProvider,
    QwenCorrectionProvider,
    load_correction_prompt,
)
from cueflow.doubao_asr_provider import DoubaoFileAsrProvider
from cueflow.edit_resolution import apply_resolved_payload
from cueflow.errors import (
    ContractError,
    CueFlowError,
    IntegrityError,
    ProviderError,
)
from cueflow.export import publish_srt
from cueflow.glm_selection_provider import (
    PROMPT_VERSION as SELECTION_PROMPT_VERSION,
)
from cueflow.glm_selection_provider import (
    GlmSelectionProvider,
    SelectionProvider,
    load_selection_prompt,
)
from cueflow.job_inputs import ReferenceSpec, build_job_input_payload
from cueflow.media import MediaBundle, prepare_media, probe_source
from cueflow.media_object_store import MediaObjectStore, TosMediaObjectStore, media_ref_from_payload
from cueflow.project import ProjectContext, single_writer
from cueflow.run_runtime import (
    _bind,
    _checkpoint_args,
    _get,
    _new_invocation,
    _record_invocation_failure,
    _require,
    _retry_identity,
    _stage,
    _succeed_with_metadata,
)
from cueflow.schema import ArtifactEnvelope, InputRef, Producer

WholeAsrFactory = Callable[[], WholeFileAsrProvider]
CorrectionFactory = Callable[[], CorrectionProvider]
MediaStoreFactory = Callable[[], MediaObjectStore]
GlmFactory = Callable[[], SelectionProvider]
AtaFactory = Callable[[], VolcengineAtaProvider]


@dataclass(frozen=True)
class _Factories:
    media: MediaStoreFactory = TosMediaObjectStore
    qwen: WholeAsrFactory = QwenFiletransProvider
    doubao: WholeAsrFactory = DoubaoFileAsrProvider
    glm: GlmFactory = GlmSelectionProvider
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
            "merge_policy": MERGE_POLICY,
            "selection_prompt_sha256": load_selection_prompt()[1],
        }
    )


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
    glm_selection_factory: GlmFactory | None = None,
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
        glm_selection_factory or GlmSelectionProvider,
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
    glm_selection_factory: GlmFactory | None = None,
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
        glm=glm_selection_factory or GlmSelectionProvider,
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
    glm_selection_factory: GlmFactory | None = None,
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
            glm_selection_factory or GlmSelectionProvider,
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
    glm_selection_factory: GlmFactory | None = None,
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
    if row["operation"] == "glm_selection" and final and final.payload["sealed"]:
        raise ContractError("sealed/human-resolved decisions cannot be overwritten by GLM retry")
    return _execute(
        context,
        run_id,
        _Factories(
            media_store_factory or TosMediaObjectStore,
            qwen_asr_factory or QwenFiletransProvider,
            doubao_asr_factory or DoubaoFileAsrProvider,
            glm_selection_factory or GlmSelectionProvider,
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
            lambda retry, key: _upload_for_run(
                context, run_id, media.timeline_audio, factories.media, retry, key
            ),
            retry_of,
        )
        _verify_media_object_for_timeline(media_object, media.timeline_audio)
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
        proposals = _correction_transcripts(
            context,
            run_id,
            job,
            base,
            peer,
            comparison,
            request,
            factories,
            retry_of,
        )
        agreement = _get(context, run_id, "merge_plan")
        if agreement is None:
            payload = build_merge_plan(
                request.base_text,
                request.peer_text,
                str(proposals[0].payload["corrected_text"]),
                str(proposals[1].payload["corrected_text"]),
            )
            agreement = _save(context, run_id, "merge_plan", payload, [base, peer, *proposals])
        final = _get(context, run_id, "edit_resolution")
        if final is None or (not final.payload["sealed"] and retry_of is not None):
            final = _selection_stage(context, run_id, base, agreement, factories.glm, retry_of)
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
                {
                    "source_text": final.payload["corrected_preview"],
                    "base_asr_artifact_id": base.artifact_id,
                    "edit_resolution_artifact_id": final.artifact_id,
                    "correction_mode": "dual_fulltext_selection",
                },
                [base, final],
            )
        ata_response = _stage(
            context,
            run_id,
            "ata",
            "ata_response",
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
                    ata_response,
                )
            ],
        )
        result = _publish_downstream(context, run_id, media, transcript, ata_response)
        context.registry.set_run_status(run_id, "succeeded")
        return result
    except BaseException as exc:
        _fail_run(context, run_id, exc)
        raise


def _upload_for_run(
    context: ProjectContext,
    run_id: str,
    timeline_audio: ArtifactEnvelope,
    factory: MediaStoreFactory,
    retry: str | None,
    key: str | None,
) -> ArtifactEnvelope:
    store = factory()
    try:
        blob = cast(Mapping[str, Any], timeline_audio.payload["audio_blob"])
        path = context.store.blob_path(str(blob["content_hash"]))
        context.store.verify_blob(path, str(blob["content_hash"]), int(blob["byte_length"]))
        return _upload_media(
            context,
            run_id,
            path,
            timeline_audio,
            store,
            retry_of=retry,
            idempotency_key=key,
        )
    finally:
        store.close()


def _correction_transcripts(
    context: ProjectContext,
    run_id: str,
    job: ArtifactEnvelope,
    base: ArtifactEnvelope,
    peer: ArtifactEnvelope,
    comparison: ArtifactEnvelope,
    request: CorrectionRequest,
    factories: _Factories,
    retry_of: str | None,
) -> list[ArtifactEnvelope]:
    inputs = [base, peer, job, comparison]
    completed: dict[str, ArtifactEnvelope] = {}
    failures: list[BaseException] = []
    pending: dict[Future[CorrectionResult], tuple[CorrectionProvider, str, int]] = {}
    prompt_hash = load_correction_prompt()[1]

    def invoke(provider: CorrectionProvider) -> CorrectionResult:
        try:
            return provider.correct(request)
        finally:
            provider.close()

    with ThreadPoolExecutor(max_workers=2, thread_name_prefix="cueflow-correction") as executor:

        def submit(
            provider: CorrectionProvider, retry: str | None, key: str | None, attempt: int
        ) -> None:
            invocation = _new_invocation(
                context,
                run_id,
                f"{provider.arm}_correction",
                provider.provider,
                provider.model,
                [(item.artifact_kind, item.artifact_id) for item in inputs],
                logical_suffix=provider.arm,
                prompt_version=PROMPT_VERSION,
                prompt_sha256=prompt_hash,
                retry_of=retry,
                idempotency_key=key,
            )
            pending[executor.submit(invoke, provider)] = (provider, invocation, attempt)

        for arm, factory in (
            ("qwen", factories.qwen_correction),
            ("kimi", factories.kimi_correction),
        ):
            artifact = _get(context, run_id, "correction_transcript", arm)
            if artifact is not None:
                completed[arm] = artifact
                continue
            try:
                retry, key = _retry_identity(context, run_id, f"{arm}_correction", arm, retry_of)
                provider = factory()
                if provider.arm != arm:
                    raise ContractError("Correction factory returned the wrong arm")
                submit(provider, retry, key, 0)
            except BaseException as exc:
                failures.append(exc)
        while pending:
            ready, _ = wait(pending, return_when=FIRST_COMPLETED)
            for future in ready:
                provider, invocation, attempt = pending.pop(future)
                try:
                    result = future.result()
                except BaseException as exc:
                    _record_invocation_failure(context, invocation, exc)
                    if isinstance(exc, CompletedResponseError) and attempt == 0:
                        factory = (
                            factories.qwen_correction
                            if provider.arm == "qwen"
                            else factories.kimi_correction
                        )
                        submit(
                            factory(),
                            invocation,
                            str(context.registry.invocation(invocation)["idempotency_key"]),
                            1,
                        )
                    else:
                        failures.append(exc)
                    continue
                envelope = ArtifactEnvelope.create(
                    artifact_kind="correction_transcript",
                    scope_key=provider.arm,
                    producer=_provider_producer(
                        provider.provider,
                        provider.model,
                        {
                            "prompt_version": PROMPT_VERSION,
                            "prompt_sha256": prompt_hash,
                            "live_search_replayable": False,
                        },
                    ),
                    inputs=[
                        InputRef(role=item.artifact_kind, artifact_id=item.artifact_id)
                        for item in inputs
                    ],
                    payload={
                        "arm": provider.arm,
                        "corrected_text": result.corrected_text,
                        "provider_metadata": result.metadata.as_dict(),
                    },
                )
                _succeed_with_metadata(context, invocation, envelope, result.metadata)
                completed[provider.arm] = envelope
    if failures:
        raise failures[0]
    return [completed["qwen"], completed["kimi"]]


def _selection_stage(
    context: ProjectContext,
    run_id: str,
    base: ArtifactEnvelope,
    plan: ArtifactEnvelope,
    factory: GlmFactory,
    retry_of: str | None,
) -> ArtifactEnvelope:
    batches, reviews = build_selection_batches(plan.payload)
    accepted = list(plan.payload["resolved_edits"])
    evidence: list[ArtifactEnvelope] = []
    cases = {item["case_id"]: item for item in plan.payload["cases"]}
    for payload in batches:
        identity = payload["batch_id"]
        batch = _get(context, run_id, "selection_batch", identity)
        if batch is None:
            prompt, digest = load_selection_prompt()
            batch = _save(
                context,
                run_id,
                "selection_batch",
                {
                    **payload,
                    "prompt": prompt,
                    "prompt_sha256": digest,
                    "prompt_version": SELECTION_PROMPT_VERSION,
                },
                [plan],
                identity,
            )
        assert batch is not None
        frozen_batch = batch
        evidence.append(batch)

        def select(
            retry: str | None, key: str | None, batch: ArtifactEnvelope = frozen_batch
        ) -> ArtifactEnvelope:
            return _select_batch(context, run_id, batch, factory, retry, key)

        try:
            outcome = _stage(
                context, run_id, "glm_selection", "selection_result", select, retry_of, identity
            )
        except (ProviderError, ContractError, TimeoutError) as exc:
            for item in batch.payload["request"]["cases"]:
                case = cases[item["case_id"]]
                reviews.append(
                    {
                        **case,
                        "review_id": "rev_" + case["case_id"],
                        "reason": "selection_unavailable",
                        "failure_type": type(exc).__name__,
                        "selection_batch_artifact_id": batch.artifact_id,
                    }
                )
            continue
        evidence.append(outcome)
        accepted.extend(
            apply_selections(
                str(base.payload["source_text"]), batch.payload, outcome.payload["decisions"]
            )
        )
    return _finalize_resolution_stage(context, run_id, base, plan, accepted, reviews, evidence)


def _select_batch(
    context: ProjectContext,
    run_id: str,
    batch: ArtifactEnvelope,
    factory: GlmFactory,
    retry: str | None,
    key: str | None,
) -> ArtifactEnvelope:
    for attempt in range(2):
        provider = factory()
        invocation = _new_invocation(
            context,
            run_id,
            "glm_selection",
            provider.provider,
            provider.model,
            [("selection_batch", batch.artifact_id)],
            logical_suffix=batch.scope_key,
            prompt_version=SELECTION_PROMPT_VERSION,
            prompt_sha256=load_selection_prompt()[1],
            retry_of=retry,
            idempotency_key=key,
        )
        try:
            result = provider.select(batch.payload["request"])
            # Validate again at the publication boundary, including custom providers.
            apply_selections(
                str(_require(context, run_id, "base_asr").payload["source_text"]),
                batch.payload,
                result.decisions,
            )
            envelope = ArtifactEnvelope.create(
                artifact_kind="selection_result",
                scope_key=batch.scope_key,
                producer=_provider_producer(
                    provider.provider,
                    provider.model,
                    {"prompt_sha256": batch.payload["prompt_sha256"], "web_search": "auto"},
                ),
                inputs=[InputRef(role="selection_batch", artifact_id=batch.artifact_id)],
                payload={
                    "batch_id": batch.scope_key,
                    "decisions": [dict(d) for d in result.decisions],
                    "request": batch.payload["request"],
                    "provider_metadata": result.metadata.as_dict(),
                },
            )
            _succeed_with_metadata(context, invocation, envelope, result.metadata)
            return envelope
        except BaseException as exc:
            _record_invocation_failure(context, invocation, exc)
            if not isinstance(exc, CompletedResponseError) or attempt == 1:
                raise
            retry, key = invocation, str(context.registry.invocation(invocation)["idempotency_key"])
        finally:
            provider.close()
    raise AssertionError("unreachable")


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
                "pending_selection": 0,
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
            fields.add("replacement")
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
        if action in {"qwen", "kimi", "peer"}:
            if action not in item.get("candidates", {}):
                raise ContractError(
                    "unavailable candidate requires keep or a manual replacement"
                )
            start, end = int(item["start"]), int(item["end"])
            replacement = str(item["candidates"][action])
        elif action == "replace":
            manual_replacement = decision.get("replacement")
            if not isinstance(manual_replacement, str):
                raise ContractError("manual replacement must be a string")
            replacement = manual_replacement
            start, end = int(item["start"]), int(item["end"])
            if base_text[start:end] != item["original"]:
                raise IntegrityError("review item no longer matches its frozen Base interval")
        else:
            raise ContractError("review action must be keep/qwen/kimi/peer/replace")
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
            _require(context, run_id, "merge_plan"),
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
        producer=_deterministic_producer("job_input", {"format": "0.5.3"}),
        inputs=[InputRef(role="source_media", source_asset_id=str(payload["source_asset_id"]))],
        payload=payload,
    )
    return context.publisher.publish(
        envelope,
        stale_targets=[
            ("correction_transcript", None),
            ("merge_plan", None),
            ("selection_batch", None),
            ("selection_result", None),
            ("review_resolution", None),
            ("edit_resolution", None),
            ("review_queue", None),
            ("transcript", None),
            ("ata_response", None),
            ("ata_result", None),
            ("srt_render", None),
        ],
    )


def _upload_media(
    context: ProjectContext,
    run_id: str,
    path: Path,
    timeline_audio: ArtifactEnvelope,
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
        [("timeline_audio", timeline_audio.artifact_id)],
        retry_of=retry_of,
        idempotency_key=idempotency_key,
    )
    try:
        ref = store.upload(path, object_name="timeline-audio.wav")
        blob = cast(Mapping[str, Any], timeline_audio.payload["audio_blob"])
        if ref.content_hash != blob["content_hash"] or ref.byte_length != blob["byte_length"]:
            raise IntegrityError("uploaded MediaObject differs from frozen TimelineAudio bytes")
        envelope = ArtifactEnvelope.create(
            artifact_kind="media_object",
            scope_key="global",
            producer=_provider_producer(store.provider, None, {"url_persisted": False}),
            inputs=[InputRef(role="timeline_audio", artifact_id=timeline_audio.artifact_id)],
            payload=ref.artifact_payload(timeline_audio.artifact_id),
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
                ("ata_response", None),
                ("ata_result", None),
                ("srt_render", None),
            ],
        )
        return envelope
    except BaseException as exc:
        _record_invocation_failure(context, invocation, exc)
        raise


def _verify_media_object_for_timeline(
    media_object: ArtifactEnvelope, timeline_audio: ArtifactEnvelope
) -> None:
    blob = cast(Mapping[str, Any], timeline_audio.payload["audio_blob"])
    if (
        media_object.payload.get("timeline_audio_artifact_id") != timeline_audio.artifact_id
        or media_object.payload.get("content_hash") != blob.get("content_hash")
        or media_object.payload.get("byte_length") != blob.get("byte_length")
        or not any(
            item.role == "timeline_audio" and item.artifact_id == timeline_audio.artifact_id
            for item in media_object.inputs
        )
    ):
        raise IntegrityError("MediaObject is not the frozen TimelineAudio object")


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
                ("correction_transcript", None),
                ("merge_plan", None),
                ("selection_batch", None),
                ("selection_result", None),
                ("edit_resolution", None),
                ("review_queue", None),
                ("transcript", None),
                ("ata_response", None),
                ("ata_result", None),
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
            ("correction_transcript", None),
            ("merge_plan", None),
            ("selection_batch", None),
            ("selection_result", None),
            ("edit_resolution", None),
            ("review_queue", None),
            ("transcript", None),
            ("ata_response", None),
            ("ata_result", None),
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
    result: AtaResponse | None = None
    try:
        result = provider.align(media_url, str(transcript.payload["source_text"]))
        content_hash, byte_length, _ = context.store.publish_bytes(result.raw_response)
        envelope = ArtifactEnvelope.create(
            artifact_kind="ata_response",
            scope_key="global",
            producer=_provider_producer(
                provider.provider, provider.model, {"sta_punc_mode": "3", "transport": "url"}
            ),
            inputs=[
                InputRef(role="media_object", artifact_id=media_object.artifact_id),
                InputRef(role="timeline_audio", artifact_id=media.timeline_audio.artifact_id),
                InputRef(role="transcript", artifact_id=transcript.artifact_id),
            ],
            payload={
                "run_id": run_id,
                "invocation_id": invocation,
                "media_object_artifact_id": media_object.artifact_id,
                "timeline_audio_artifact_id": media.timeline_audio.artifact_id,
                "transcript_artifact_id": transcript.artifact_id,
                "audio_text": result.audio_text,
                "provider_metadata": result.metadata.as_dict(),
                "response_blob": {
                    "content_hash": content_hash,
                    "byte_length": byte_length,
                    "media_type": "application/json",
                },
            },
        )
        _succeed_with_metadata(
            context, invocation, envelope, result.metadata,
            stale_targets=[("ata_result", None), ("srt_render", None)],
        )
        return envelope
    except BaseException as exc:
        if result is not None and getattr(exc, "metadata", None) is None:
            cast(Any, exc).metadata = result.metadata
        _record_invocation_failure(context, invocation, exc)
        raise
    finally:
        provider.close()


def _publish_downstream(
    context: ProjectContext,
    run_id: str,
    media: MediaBundle,
    transcript: ArtifactEnvelope,
    ata_response: ArtifactEnvelope,
) -> dict[str, Any]:
    # The paid response/checkpoint is already committed. These steps are local and replayable.
    result = _get(context, run_id, "ata_result")
    if result is None:
        blob = ata_response.payload["response_blob"]
        path = context.store.blob_path(blob["content_hash"])
        context.store.verify_blob(path, blob["content_hash"], blob["byte_length"])
        utterances = parse_ata_result(path.read_bytes())
        result = _save(
            context, run_id, "ata_result",
            {
                "run_id": run_id,
                "ata_response_artifact_id": ata_response.artifact_id,
                "media_object_artifact_id": ata_response.payload["media_object_artifact_id"],
                "timeline_audio_artifact_id": media.timeline_audio.artifact_id,
                "transcript_artifact_id": transcript.artifact_id,
                "utterances": utterances,
                "diagnostics": ata_diagnostics(ata_response.payload["audio_text"], utterances),
            },
            [ata_response, transcript, media.timeline_audio],
        )
    context.registry.activate_artifacts(context.project_id, [result.artifact_id])
    render, output = publish_srt(
        context,
        run_id=run_id,
        timeline_audio=media.timeline_audio,
        transcript=transcript,
        ata_response=ata_response,
        ata_result=result,
    )
    return {
        "status": "succeeded",
        "run_id": run_id,
        "base_asr_artifact_id": transcript.payload["base_asr_artifact_id"],
        "transcript_artifact_id": transcript.artifact_id,
        "ata_response_artifact_id": ata_response.artifact_id,
        "ata_result_artifact_id": result.artifact_id,
        "diagnostics": result.payload["diagnostics"],
        "srt_render_artifact_id": render.artifact_id,
        "output_path": str(output.resolve()),
    }


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
