from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

import cueflow.reference_orchestrator as reference_orchestrator
from cueflow.errors import ContractError, ReferenceRunFailedError
from cueflow.orchestrator import project_status
from cueflow.project import ProjectContext
from cueflow.reference_assets import register_reference_asset
from cueflow.reference_media import ReferenceWorkSpec
from cueflow.reference_orchestrator import (
    ReferenceProviders,
    extract_reference,
    reference_status,
    retry_reference_work_item,
)
from cueflow.reference_providers import (
    CloudReferenceAsr,
    CloudReferenceVision,
    QwenCloudDocumentParser,
    ReferenceModelResult,
    ReferenceVisionRequest,
)


def _result(text: str) -> ReferenceModelResult:
    return ReferenceModelResult(
        text=text,
        segments=(),
        response_id="response-" + text,
        provider_usage={"prompt_tokens": 1},
        provider_usage_duration=None,
        provider_cost=None,
    )


def test_default_reference_provider_pool_is_lazy_and_remote() -> None:
    pool = reference_orchestrator._ProviderPool(None)
    assert pool._owned == []
    assert isinstance(pool.asr(), CloudReferenceAsr)
    assert isinstance(pool.vision(), CloudReferenceVision)
    assert isinstance(pool.document(), QwenCloudDocumentParser)
    assert pool.asr() is pool.providers.asr
    assert len(pool._owned) == 3
    pool.close()


class FakeVision:
    provider = "fake-provider"
    model = "qwen3.7-plus"

    def __init__(self, outcomes: list[ReferenceModelResult | Exception]) -> None:
        self.outcomes = outcomes
        self.calls: list[ReferenceVisionRequest] = []

    def recognize(self, request: ReferenceVisionRequest) -> ReferenceModelResult:
        self.calls.append(request)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    def close(self) -> None:
        return None


class PreflightFailureVision(FakeVision):
    def preflight(self) -> None:
        raise RuntimeError("provider preflight failed before a call")


def _fake_image_conversion(
    path: Path, _runtime: Any, output: Path
) -> tuple[Path, tuple[str, ...], str]:
    output.write_bytes(b"stable converted JPEG bytes")
    return (
        output,
        ("ffmpeg", "-i", str(path), "-q:v", "8", str(output)),
        "stable-encoded-sha256",
    )


def _png(path: Path) -> None:
    path.write_bytes(bytes.fromhex("89504E470D0A1A0A") + b"fixture")


def test_each_extract_creates_new_run_and_overwrite_reads_current_file(
    tmp_path: Path,
) -> None:
    context = ProjectContext.create(tmp_path / "project", "Runs")
    path = tmp_path / "notes.txt"
    path.write_text("first version", encoding="utf-8")
    reference = register_reference_asset(context, path)
    try:
        first = extract_reference(context, reference["reference_asset_id"])
        first_item = context.registry.reference_work_items_for_run(first["run_id"])[0]
        first_evidence = context.artifact(str(first_item["evidence_artifact_id"]))

        path.write_text("second version", encoding="utf-8")
        duplicate = register_reference_asset(context, path)
        second = extract_reference(context, reference["reference_asset_id"])
        second_item = context.registry.reference_work_items_for_run(second["run_id"])[0]
        second_evidence = context.artifact(str(second_item["evidence_artifact_id"]))

        assert duplicate["reference_asset_id"] == reference["reference_asset_id"]
        assert first["run_id"] != second["run_id"]
        assert "first version" in first_evidence.payload["content"]["blocks"][0]["text"]
        assert "second version" in second_evidence.payload["content"]["blocks"][0]["text"]
        assert len(context.registry.reference_runs(reference["reference_asset_id"])) == 2
        assert context.registry.invocations_for_run(first["run_id"]) == []
        assert context.registry.invocations_for_run(second["run_id"]) == []
        assert "content_hash" not in first_evidence.payload["provenance"]
        artifact_kinds = {
            str(row[0])
            for row in context.registry._connection.execute(
                "SELECT DISTINCT artifact_kind FROM artifacts"
            )
        }
        assert artifact_kinds == {
            "reference_input",
            "reference_evidence",
            "reference_bundle",
            "lexicon_input",
        }
        assert context.registry._connection.execute(
            "SELECT COUNT(*) FROM source_assets"
        ).fetchone()[0] == 0
        statuses = reference_status(context, reference["reference_asset_id"])
        assert [run["run_id"] for run in statuses["reference_assets"][0]["runs"]] == [
            first["run_id"],
            second["run_id"],
        ]
    finally:
        context.close()


def test_reference_run_never_overwrites_latest_source_run_status(tmp_path: Path) -> None:
    context = ProjectContext.create(tmp_path / "project", "Status")
    source_run = context.registry.create_source_run(
        context.project_id, {"source_asset_id": "source"}, "sha256:" + "0" * 64
    )
    context.registry.set_run_status(source_run, "succeeded")
    path = tmp_path / "notes.txt"
    path.write_text("reference", encoding="utf-8")
    reference = register_reference_asset(context, path)
    try:
        result = extract_reference(context, reference["reference_asset_id"])
        status = project_status(context)
        assert status["latest_source_run"]["run_id"] == source_run
        assert status["latest_source_run"]["status"] == "succeeded"
        assert "latest_run" not in status
        assert status["reference_runs"][0]["run_id"] == result["run_id"]
    finally:
        context.close()


def test_partial_retry_stays_in_original_run_and_reuses_successful_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    context = ProjectContext.create(tmp_path / "project", "Retry")
    path = tmp_path / "reference.png"
    _png(path)
    reference = register_reference_asset(context, path)
    monkeypatch.setattr(
        reference_orchestrator,
        "_plan_work",
        lambda *_args, **_kwargs: (
            ReferenceWorkSpec("image_visual", "image_visual", "image_vision", {}),
            ReferenceWorkSpec(
                "burned_subtitle", "burned_subtitle", "image_vision", {}
            ),
        ),
    )
    monkeypatch.setattr(
        reference_orchestrator, "prepare_visual_image", _fake_image_conversion
    )
    provider = FakeVision([_result("first succeeded"), RuntimeError("second failed")])
    supplied = ReferenceProviders(vision=provider)
    try:
        with pytest.raises(ReferenceRunFailedError) as failure:
            extract_reference(
                context,
                reference["reference_asset_id"],
                providers=supplied,
            )
        run_id = failure.value.run_id
        assert failure.value.outcome == "partial"
        runs_before = context.registry.reference_runs(reference["reference_asset_id"])
        assert [row["run_id"] for row in runs_before] == [run_id]
        items = context.registry.reference_work_items_for_run(run_id)
        successful = next(row for row in items if row["status"] == "succeeded")
        failed = next(row for row in items if row["status"] == "failed")
        old_evidence_id = str(successful["evidence_artifact_id"])
        old_bundle_id = str(context.registry.reference_run(run_id)["current_bundle_artifact_id"])
        assert context.artifact(old_bundle_id).payload["outcome"] == "partial"

        provider.outcomes.append(_result("retry succeeded"))
        result = retry_reference_work_item(
            context, str(failed["work_item_id"]), providers=supplied
        )
        assert result["run_id"] == run_id
        assert result["outcome"] == "complete"
        assert len(context.registry.reference_runs(reference["reference_asset_id"])) == 1
        assert result["bundle_artifact_id"] != old_bundle_id
        new_bundle = context.artifact(str(result["bundle_artifact_id"]))
        assert old_evidence_id in new_bundle.payload["evidence_artifact_ids"]
        assert len(new_bundle.payload["evidence_artifact_ids"]) == 2
        assert str(
            context.registry.reference_work_item(str(successful["work_item_id"]))[
                "evidence_artifact_id"
            ]
        ) == old_evidence_id

        invocations = context.registry.reference_invocations_for_work_item(
            str(failed["work_item_id"])
        )
        assert len(invocations) == 2
        assert invocations[1]["retry_parent_invocation_id"] == invocations[0][
            "invocation_id"
        ]
        assert invocations[1]["retry_reason"] == "explicit_work_item_retry"
        details = context.registry.reference_invocation_details(
            str(invocations[1]["invocation_id"])
        )
        assert details["provider_cost"] is None
        assert details["local_measured_duration"] is None
        assert details["provider_usage_duration"] is None
        assert '"jpeg_qv": 8' in details["actual_config_json"]

        with pytest.raises(ContractError, match="failed or interrupted"):
            retry_reference_work_item(
                context, str(failed["work_item_id"]), providers=supplied
            )
    finally:
        context.close()


def test_model_work_item_never_exceeds_two_sent_attempts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    context = ProjectContext.create(tmp_path / "project", "Budget")
    path = tmp_path / "reference.png"
    _png(path)
    reference = register_reference_asset(context, path)
    monkeypatch.setattr(
        reference_orchestrator, "prepare_visual_image", _fake_image_conversion
    )
    provider = FakeVision([RuntimeError("first"), RuntimeError("retry")])
    supplied = ReferenceProviders(vision=provider)
    try:
        with pytest.raises(ReferenceRunFailedError) as failure:
            extract_reference(
                context, reference["reference_asset_id"], providers=supplied
            )
        run_id = failure.value.run_id
        work_item_id = str(
            context.registry.reference_work_items_for_run(run_id)[0]["work_item_id"]
        )
        with pytest.raises(ReferenceRunFailedError):
            retry_reference_work_item(context, work_item_id, providers=supplied)
        assert context.registry.sent_reference_attempt_count(work_item_id) == 2
        assert len(context.registry.reference_invocations_for_work_item(work_item_id)) == 2

        with pytest.raises(ReferenceRunFailedError):
            retry_reference_work_item(context, work_item_id, providers=supplied)
        assert len(provider.calls) == 2
        assert len(context.registry.reference_invocations_for_work_item(work_item_id)) == 2
        assert len(context.registry.reference_runs(reference["reference_asset_id"])) == 1
    finally:
        context.close()


def test_preflight_failure_creates_no_invocation_and_retry_reuses_input_artifact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    context = ProjectContext.create(tmp_path / "project", "Preflight")
    path = tmp_path / "reference.png"
    _png(path)
    reference = register_reference_asset(context, path)
    monkeypatch.setattr(
        reference_orchestrator, "prepare_visual_image", _fake_image_conversion
    )
    try:
        with pytest.raises(ReferenceRunFailedError) as failure:
            extract_reference(
                context,
                reference["reference_asset_id"],
                providers=ReferenceProviders(
                    vision=PreflightFailureVision([_result("must not be called")])
                ),
            )
        run_id = failure.value.run_id
        item = context.registry.reference_work_items_for_run(run_id)[0]
        work_item_id = str(item["work_item_id"])
        assert context.registry.reference_invocations_for_work_item(work_item_id) == []
        input_rows = context.registry.reference_input_artifacts(context.project_id)
        assert len(input_rows) == 1
        original_input_id = str(input_rows[0]["artifact_id"])

        result = retry_reference_work_item(
            context,
            work_item_id,
            providers=ReferenceProviders(vision=FakeVision([_result("retry")])),
        )
        assert result["run_id"] == run_id
        invocation = context.registry.reference_invocations_for_work_item(work_item_id)[0]
        assert context.registry.invocation_inputs(str(invocation["invocation_id"]))[0][
            "input_artifact_id"
        ] == original_input_id
        assert len(context.registry.reference_input_artifacts(context.project_id)) == 1
    finally:
        context.close()
