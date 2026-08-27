from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from typing import Any, cast

from cueflow.canonical import hash_json
from cueflow.config import (
    COMPONENT_VERSION,
    LEXICON_BATCH_MAX_CHARACTERS,
    LEXICON_MODEL_SENT_ATTEMPT_LIMIT,
)
from cueflow.errors import (
    ContractError,
    DeliveryAmbiguousError,
    LexiconRunFailedError,
)
from cueflow.lexicon import ingest_candidate_occurrences
from cueflow.lexicon_providers import (
    CloudLexiconExtractor,
    LexiconExtractionRequest,
    LexiconExtractor,
)
from cueflow.project import ProjectContext
from cueflow.schema import ArtifactEnvelope, InputRef, Producer
from cueflow.term_candidates import (
    LEXICON_NORMALIZATION_VERSION,
    CandidateOccurrence,
    EvidenceUnit,
    ValidatedOccurrence,
    evidence_units,
    validate_occurrence,
)

LexiconFactory = Callable[[], LexiconExtractor]
_SLICE_OVERLAP = 128


def discover_terms_for_bundle(
    context: ProjectContext,
    reference_bundle: ArtifactEnvelope,
    *,
    extractor_factory: LexiconFactory | None = None,
) -> dict[str, Any]:
    context.registry.recover_running_lexicon_runs()
    if reference_bundle.artifact_kind != "reference_bundle":
        raise ContractError("terminology discovery requires a Reference Bundle")
    evidence_ids = _bundle_evidence_ids(reference_bundle)
    new_evidence_ids = [
        artifact_id
        for artifact_id in evidence_ids
        if context.registry.lexicon_coverage(artifact_id) is None
    ]
    if not new_evidence_ids:
        return {
            "status": "no_new_evidence",
            "run_id": None,
            "outcome": "complete",
            "processed_evidence_artifact_ids": [],
        }
    prepared_batches: list[tuple[str, list[list[dict[str, Any]]]]] = []
    for evidence_id in new_evidence_ids:
        units = evidence_units(context.artifact(evidence_id))
        batches = _batch_units(units)
        if not batches:
            raise ContractError("Reference Evidence contains no terminology input text")
        prepared_batches.append((evidence_id, batches))
    trigger_reference_run_id = str(reference_bundle.payload["run_id"])
    run_config = {
        "normalization_version": LEXICON_NORMALIZATION_VERSION,
        "batch_max_characters": LEXICON_BATCH_MAX_CHARACTERS,
        "slice_overlap": _SLICE_OVERLAP,
    }
    run_id = context.registry.create_lexicon_run(
        context.project_id,
        trigger_reference_run_id,
        reference_bundle.artifact_id,
        {
            "trigger_reference_run_id": trigger_reference_run_id,
            "reference_bundle_artifact_id": reference_bundle.artifact_id,
            "evidence_artifact_ids": new_evidence_ids,
        },
        hash_json(run_config),
    )
    manifest_batches: list[dict[str, Any]] = []
    ordinal = 0
    for evidence_id, batches in prepared_batches:
        context.registry.create_lexicon_coverage(
            run_id=run_id, evidence_artifact_id=evidence_id
        )
        for batch_ordinal, batch in enumerate(batches):
            batch_manifest = {
                "evidence_artifact_id": evidence_id,
                "batch_ordinal": batch_ordinal,
                "units": batch,
            }
            work_item_id = context.registry.create_lexicon_work_item(
                run_id=run_id,
                ordinal=ordinal,
                evidence_artifact_id=evidence_id,
                batch_ordinal=batch_ordinal,
                batch_manifest=batch_manifest,
            )
            manifest_batches.append({"work_item_id": work_item_id, **batch_manifest})
            ordinal += 1
    manifest = _publish_lexicon_input(
        context,
        run_id=run_id,
        trigger_reference_run_id=trigger_reference_run_id,
        reference_bundle=reference_bundle,
        evidence_ids=new_evidence_ids,
        batches=manifest_batches,
        config=run_config,
    )
    context.registry.set_lexicon_input_manifest(run_id, manifest.artifact_id)
    context.registry.set_run_status(run_id, "running")
    try:
        extractor = (extractor_factory or CloudLexiconExtractor)()
    except Exception as exc:
        for row in context.registry.lexicon_work_items_for_run(run_id):
            context.registry.set_lexicon_work_item_status(
                str(row["work_item_id"]),
                "failed",
                failure_code=type(exc).__name__,
                failure_details={"message": str(exc), "stage": "provider_initialization"},
            )
            context.registry.refresh_lexicon_coverage(str(row["evidence_artifact_id"]))
        return _finalize_lexicon_run(context, run_id)
    try:
        for row in context.registry.lexicon_work_items_for_run(run_id):
            _execute_work_item(
                context,
                dict(row),
                manifest=manifest,
                extractor=extractor,
                retry_reason=None,
            )
    finally:
        extractor.close()
    return _finalize_lexicon_run(context, run_id)


def retry_suggestion_work_item(
    context: ProjectContext,
    work_item_id: str,
    *,
    extractor_factory: LexiconFactory | None = None,
) -> dict[str, Any]:
    context.registry.recover_running_lexicon_runs()
    item = context.registry.lexicon_work_item(work_item_id)
    if item["status"] not in {"failed", "interrupted"}:
        raise ContractError("Suggested Terms retry requires a failed or interrupted work item")
    if context.registry.sent_lexicon_attempt_count(work_item_id) >= (
        LEXICON_MODEL_SENT_ATTEMPT_LIMIT
    ):
        raise ContractError("Lexicon model sent-attempt limit exhausted")
    run_id = str(item["run_id"])
    run = context.registry.lexicon_run(run_id)
    manifest_id = run["input_manifest_artifact_id"]
    if manifest_id is None:
        raise ContractError("Lexicon Run has no bound input manifest")
    manifest = context.artifact(str(manifest_id))
    context.registry.reopen_run_for_retry(run_id)
    extractor = (extractor_factory or CloudLexiconExtractor)()
    try:
        _execute_work_item(
            context,
            dict(context.registry.lexicon_work_item(work_item_id)),
            manifest=manifest,
            extractor=extractor,
            retry_reason="explicit_work_item_retry",
        )
    finally:
        extractor.close()
    result = _finalize_lexicon_run(context, run_id)
    if result["outcome"] != "complete":
        raise LexiconRunFailedError(run_id, str(result["outcome"]))
    return result


def suggestion_status(context: ProjectContext) -> dict[str, Any]:
    return {
        "jobs": [
            {
                "run_id": str(run["run_id"]),
                "trigger_reference_run_id": str(run["trigger_reference_run_id"]),
                "status": str(run["status"]),
                "outcome": run["outcome"],
                "error_message": run["error_message"],
                "work_items": [
                    {
                        "work_item_id": str(item["work_item_id"]),
                        "evidence_artifact_id": str(item["evidence_artifact_id"]),
                        "batch_ordinal": int(item["batch_ordinal"]),
                        "status": str(item["status"]),
                        "failure_code": item["failure_code"],
                    }
                    for item in context.registry.lexicon_work_items_for_run(
                        str(run["run_id"])
                    )
                ],
            }
            for run in context.registry.lexicon_runs()
        ]
    }


def suggestion_jobs_for_reference_run(
    context: ProjectContext, reference_run_id: str
) -> list[dict[str, Any]]:
    return [
        {
            "run_id": str(run["run_id"]),
            "status": str(run["status"]),
            "outcome": run["outcome"],
            "error_message": run["error_message"],
        }
        for run in context.registry.lexicon_runs()
        if run["trigger_reference_run_id"] == reference_run_id
    ]


def _execute_work_item(
    context: ProjectContext,
    item: Mapping[str, Any],
    *,
    manifest: ArtifactEnvelope,
    extractor: LexiconExtractor,
    retry_reason: str | None,
) -> bool:
    work_item_id = str(item["work_item_id"])
    evidence_id = str(item["evidence_artifact_id"])
    run_id = str(item["run_id"])
    evidence = context.artifact(evidence_id)
    units = evidence_units(evidence)
    batch_manifest = _mapping_json(str(item["batch_manifest_json"]), "batch manifest")
    request_units = _request_units(units, batch_manifest)
    previous = context.registry.lexicon_invocations_for_work_item(work_item_id)
    retry_parent = str(previous[-1]["invocation_id"]) if previous else None
    invocation_id = context.registry.create_lexicon_invocation(
        work_item_id=work_item_id,
        run_id=run_id,
        project_id=context.project_id,
        provider=extractor.provider,
        model=extractor.model,
        actual_config={
            "normalization_version": LEXICON_NORMALIZATION_VERSION,
            "batch_max_characters": LEXICON_BATCH_MAX_CHARACTERS,
        },
        inputs=(
            ("lexicon_input", manifest.artifact_id),
            ("reference_evidence", evidence_id),
        ),
        retry_parent_invocation_id=retry_parent,
        retry_reason=retry_reason,
    )
    context.registry.set_lexicon_work_item_status(work_item_id, "running")
    preflight = getattr(extractor, "preflight", None)
    try:
        if callable(preflight):
            preflight()
    except Exception as exc:
        context.registry.set_invocation_status(
            invocation_id,
            "definitely_not_sent",
            error_message=str(exc) or type(exc).__name__,
        )
        context.registry.update_lexicon_invocation_details(
            invocation_id,
            failure_code=type(exc).__name__,
            failure_details={"message": str(exc), "stage": "preflight"},
        )
        context.registry.set_lexicon_work_item_status(
            work_item_id,
            "failed",
            failure_code=type(exc).__name__,
            failure_details={"message": str(exc), "stage": "preflight"},
        )
        context.registry.refresh_lexicon_coverage(evidence_id)
        return False
    context.registry.set_invocation_status(invocation_id, "sending")
    try:
        result = extractor.extract(
            LexiconExtractionRequest(
                evidence_artifact_id=evidence_id,
                evidence_role=str(evidence.payload["evidence_role"]),
                units=tuple(request_units),
            )
        )
        validated = _validate_result(result.occurrences, units, request_units)
        observations = ingest_candidate_occurrences(
            context,
            evidence_artifact_id=evidence_id,
            reference_role=str(evidence.payload["evidence_role"]),
            occurrences=validated,
        )
        candidate_set = _publish_candidate_set(
            context,
            run_id=run_id,
            work_item_id=work_item_id,
            evidence=evidence,
            manifest=manifest,
            observations=observations,
            extractor=extractor,
        )
    except Exception as exc:
        status = (
            "delivery_ambiguous"
            if isinstance(exc, DeliveryAmbiguousError)
            else "explicit_failure"
        )
        context.registry.set_invocation_status(
            invocation_id, status, error_message=str(exc) or type(exc).__name__
        )
        context.registry.update_lexicon_invocation_details(
            invocation_id,
            failure_code=type(exc).__name__,
            failure_details={"message": str(exc)},
        )
        context.registry.set_lexicon_work_item_status(
            work_item_id,
            "failed",
            failure_code=type(exc).__name__,
            failure_details={"message": str(exc)},
        )
        context.registry.refresh_lexicon_coverage(evidence_id)
        return False
    context.registry.update_lexicon_invocation_details(
        invocation_id,
        response_id=result.response_id,
        provider_usage=result.provider_usage,
        provider_cost=result.provider_cost,
    )
    context.registry.set_invocation_status(
        invocation_id,
        "succeeded",
        response_id=result.response_id,
        artifact_id=candidate_set.artifact_id,
    )
    context.registry.set_lexicon_work_item_status(
        work_item_id,
        "succeeded",
        candidate_set_artifact_id=candidate_set.artifact_id,
    )
    context.registry.refresh_lexicon_coverage(evidence_id)
    return True


def _validate_result(
    occurrences: Sequence[CandidateOccurrence],
    full_units: Sequence[EvidenceUnit],
    request_units: Sequence[Mapping[str, Any]],
) -> list[ValidatedOccurrence]:
    validated: list[ValidatedOccurrence] = []
    for occurrence in occurrences:
        matching = [
            unit
            for unit in request_units
            if tuple(cast(Sequence[str | int], unit["field_path"]))
            == occurrence.field_path
            and str(unit["text"])[occurrence.start_offset : occurrence.end_offset]
            == occurrence.raw_surface_form
        ]
        if len(matching) != 1:
            raise ContractError(
                "candidate does not identify exactly one supplied Evidence batch unit"
            )
        base = int(matching[0]["base_offset"])
        rebased = CandidateOccurrence(
            raw_surface_form=occurrence.raw_surface_form,
            field_path=occurrence.field_path,
            start_offset=base + occurrence.start_offset,
            end_offset=base + occurrence.end_offset,
            category=occurrence.category,
            proper_noun_subtype=occurrence.proper_noun_subtype,
            suggested_surface_form=occurrence.suggested_surface_form,
            risk_tags=occurrence.risk_tags,
        )
        validated.append(validate_occurrence(rebased, full_units))
    return validated


def _publish_lexicon_input(
    context: ProjectContext,
    *,
    run_id: str,
    trigger_reference_run_id: str,
    reference_bundle: ArtifactEnvelope,
    evidence_ids: Sequence[str],
    batches: Sequence[Mapping[str, Any]],
    config: Mapping[str, Any],
) -> ArtifactEnvelope:
    envelope = ArtifactEnvelope.create(
        artifact_kind="lexicon_input",
        scope_key=run_id,
        producer=Producer(
            component="lexicon-input",
            component_version=COMPONENT_VERSION,
            provider=None,
            model=None,
            config_hash=hash_json(config),
        ),
        inputs=(
            InputRef(role="reference_bundle", artifact_id=reference_bundle.artifact_id),
            *(
                InputRef(role="reference_evidence", artifact_id=evidence_id)
                for evidence_id in evidence_ids
            ),
        ),
        payload={
            "run_id": run_id,
            "trigger_reference_run_id": trigger_reference_run_id,
            "reference_bundle_artifact_id": reference_bundle.artifact_id,
            "normalization_version": LEXICON_NORMALIZATION_VERSION,
            "batches": [dict(batch) for batch in batches],
        },
    )
    return context.publisher.publish(envelope, make_current=False)


def _publish_candidate_set(
    context: ProjectContext,
    *,
    run_id: str,
    work_item_id: str,
    evidence: ArtifactEnvelope,
    manifest: ArtifactEnvelope,
    observations: Sequence[Mapping[str, Any]],
    extractor: LexiconExtractor,
) -> ArtifactEnvelope:
    envelope = ArtifactEnvelope.create(
        artifact_kind="term_candidate_set",
        scope_key=work_item_id,
        producer=Producer(
            component="term-candidate-extraction",
            component_version=COMPONENT_VERSION,
            provider=extractor.provider,
            model=extractor.model,
            config_hash=hash_json(
                {"normalization_version": LEXICON_NORMALIZATION_VERSION}
            ),
        ),
        inputs=(
            InputRef(role="lexicon_input", artifact_id=manifest.artifact_id),
            InputRef(role="reference_evidence", artifact_id=evidence.artifact_id),
        ),
        payload={
            "run_id": run_id,
            "work_item_id": work_item_id,
            "evidence_artifact_id": evidence.artifact_id,
            "candidates": [dict(item) for item in observations],
        },
    )
    return context.publisher.publish(envelope, make_current=False)


def _finalize_lexicon_run(context: ProjectContext, run_id: str) -> dict[str, Any]:
    items = [dict(row) for row in context.registry.lexicon_work_items_for_run(run_id)]
    successes = [row for row in items if row["status"] == "succeeded"]
    failures = [row for row in items if row["status"] != "succeeded"]
    if successes and not failures:
        status, outcome = "succeeded", "complete"
    elif successes:
        status, outcome = "failed", "partial"
    else:
        status, outcome = "failed", "failed"
    error_message = (
        None if not failures else f"{len(failures)} of {len(items)} term batches failed"
    )
    context.registry.set_lexicon_run_result(
        run_id, status=status, outcome=outcome, error_message=error_message
    )
    return {
        "status": status,
        "run_id": run_id,
        "outcome": outcome,
        "successful_work_item_count": len(successes),
        "failed_work_item_count": len(failures),
    }


def _bundle_evidence_ids(bundle: ArtifactEnvelope) -> list[str]:
    value = bundle.payload.get("evidence_artifact_ids")
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ContractError("Reference Bundle evidence IDs are invalid")
    return list(dict.fromkeys(value))


def _batch_units(units: Sequence[EvidenceUnit]) -> list[list[dict[str, Any]]]:
    pieces: list[dict[str, Any]] = []
    for unit in units:
        if not unit.text:
            continue
        start = 0
        while start < len(unit.text):
            end = min(len(unit.text), start + LEXICON_BATCH_MAX_CHARACTERS)
            text = unit.text[start:end]
            pieces.append(
                {
                    "field_path": list(unit.field_path),
                    "source_start_offset": start,
                    "source_end_offset": end,
                    "text_hash": hash_json({"text": text}),
                    "coordinates": dict(unit.coordinates),
                }
            )
            if end == len(unit.text):
                break
            start = end - _SLICE_OVERLAP
    batches: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    current_size = 0
    for piece in pieces:
        size = int(piece["source_end_offset"]) - int(piece["source_start_offset"])
        if current and current_size + size > LEXICON_BATCH_MAX_CHARACTERS:
            batches.append(current)
            current = []
            current_size = 0
        current.append(piece)
        current_size += size
    if current:
        batches.append(current)
    return batches


def _request_units(
    full_units: Sequence[EvidenceUnit], batch_manifest: Mapping[str, Any]
) -> list[dict[str, Any]]:
    raw_units = batch_manifest.get("units")
    if not isinstance(raw_units, list):
        raise ContractError("Lexicon batch manifest units are invalid")
    result: list[dict[str, Any]] = []
    for raw in raw_units:
        if not isinstance(raw, Mapping):
            raise ContractError("Lexicon batch manifest unit is invalid")
        path_value = raw.get("field_path")
        if not isinstance(path_value, list):
            raise ContractError("Lexicon batch manifest field_path is invalid")
        path = tuple(cast(Sequence[str | int], path_value))
        matches = [unit for unit in full_units if unit.field_path == path]
        if len(matches) != 1:
            raise ContractError("Lexicon batch manifest does not map to Evidence")
        start = int(raw["source_start_offset"])
        end = int(raw["source_end_offset"])
        text = matches[0].text[start:end]
        if hash_json({"text": text}) != raw.get("text_hash"):
            raise ContractError("Lexicon batch text hash does not match bound Evidence")
        result.append(
            {
                "field_path": list(path),
                "base_offset": start,
                "text": text,
                "coordinates": dict(matches[0].coordinates),
            }
        )
    return result


def _mapping_json(value: str, name: str) -> Mapping[str, Any]:
    try:
        result = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ContractError(f"{name} is invalid JSON") from exc
    if not isinstance(result, Mapping):
        raise ContractError(f"{name} must be an object")
    return cast(Mapping[str, Any], result)
