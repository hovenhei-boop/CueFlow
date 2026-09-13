from __future__ import annotations

import uuid
from collections.abc import Callable, Mapping, Sequence

from cueflow.asr_contracts import ProviderMetadata
from cueflow.canonical import hash_json
from cueflow.errors import (
    ContractError,
    DeliveryAmbiguousError,
    IntegrityError,
    ProviderError,
    ProviderUnavailableError,
)
from cueflow.project import RunContext
from cueflow.schema import ArtifactEnvelope


def _checkpoint_args(
    context: RunContext,
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
                "execution_round": context.registry.round_number(run_id, stage),
            }
        ),
    )


def _bind(context: RunContext, run_id: str, artifact: ArtifactEnvelope) -> ArtifactEnvelope:
    args = _checkpoint_args(context, run_id, artifact.artifact_kind, artifact.scope_key)
    context.registry.bind_checkpoint(args[0], args[1], artifact.artifact_id, args[3], args[2])
    return artifact


def _get(
    context: RunContext,
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


def _require(context: RunContext, run_id: str, stage: str) -> ArtifactEnvelope:
    result = _get(context, run_id, stage)
    if result is None:
        raise IntegrityError(f"missing run checkpoint: {stage}")
    return result


def _stage(
    context: RunContext,
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
    retry, key = _retry_identity(context, run_id, operation, scope, retry_of)
    return action(retry, key)


def _retry_identity(
    context: RunContext,
    run_id: str,
    operation: str,
    scope: str,
    retry_of: str | None,
) -> tuple[str | None, str | None]:
    rows = [
        row
        for row in context.registry.invocations_for_run(run_id)
        if row["logical_operation_key"] == f"{operation}:{scope}"
        and row["execution_round"] == context.registry.round_number(run_id)
    ]
    latest = rows[-1] if rows else None
    if latest is not None:
        if latest["status"] == "succeeded":
            raise IntegrityError("successful invocation is missing its atomic checkpoint")
        resumable_remote = latest["remote_job_id"] and latest["status"] == "delivery_ambiguous"
        if latest["invocation_id"] != retry_of and not resumable_remote:
            raise ProviderError(f"{operation}/{scope} previously failed; explicit retry required")
        if latest["status"] not in {
            "explicit_failure",
            "definitely_not_sent",
            "delivery_ambiguous",
        }:
            raise ContractError("only the latest failed attempt can be retried")
    return (
        str(latest["invocation_id"]) if latest else None,
        str(latest["idempotency_key"]) if latest else None,
    )


def _new_invocation(
    context: RunContext,
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
    from cueflow.lifecycle import check_cancellation

    check_cancellation(context)
    context.execution_control.before_invocation(run_id, operation)
    if retry_of is None and operation in {"qwen_asr", "doubao_asr", "ata"}:
        candidates = context.registry.invocations_for_run(run_id)
        for candidate in reversed(candidates):
            if (candidate["operation"] != operation or not candidate["remote_job_id"]
                    or candidate["status"] != "delivery_ambiguous"):
                continue
            bound = [(str(row["role"]), str(row["input_artifact_id"]))
                     for row in context.registry.invocation_inputs(candidate["invocation_id"])]
            if bound == list(inputs):
                retry_of = str(candidate["invocation_id"])
                idempotency_key = str(candidate["idempotency_key"])
            break
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
        owner_run_id=context.run_id,
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
    context: RunContext, invocation: str, exc: BaseException
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
    raw_metadata = getattr(exc, "metadata", None)
    metadata = raw_metadata if isinstance(raw_metadata, ProviderMetadata) else None
    diagnostic_method = getattr(exc, "diagnostic", None)
    raw_diagnostic = diagnostic_method() if callable(diagnostic_method) else None
    diagnostic = dict(raw_diagnostic) if isinstance(raw_diagnostic, Mapping) else None
    if diagnostic is not None and metadata and metadata.search_results:
        diagnostic["search_results"] = [dict(item) for item in metadata.search_results]
    context.registry.set_invocation_status(
        invocation,
        status,
        error_message=str(exc),
        response_id=metadata.response_id if metadata else None,
        resolved_model=metadata.resolved_model if metadata else None,
        elapsed_ms=metadata.elapsed_ms if metadata else None,
        reasoning_ms=metadata.reasoning_ms if metadata else None,
        usage=metadata.usage if metadata else None,
        diagnostic=diagnostic,
    )


def _succeed_with_metadata(
    context: RunContext,
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
