from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

import cueflow.reference_orchestrator as reference_orchestrator
from cueflow.errors import (
    ContractError,
    DeliveryAmbiguousError,
    LexiconRunFailedError,
    ReferenceRunFailedError,
)
from cueflow.lexicon import list_suggestions
from cueflow.lexicon_orchestrator import (
    discover_terms_for_bundle,
    retry_suggestion_work_item,
)
from cueflow.lexicon_providers import (
    LexiconExtractionRequest,
    LexiconExtractionResult,
)
from cueflow.orchestrator import initialize_project, project_status
from cueflow.project import ProjectContext
from cueflow.reference_assets import register_reference_asset
from cueflow.reference_media import ReferenceWorkSpec
from cueflow.reference_orchestrator import (
    ReferenceProviders,
    extract_reference,
    retry_reference_work_item,
)
from cueflow.reference_providers import ReferenceModelResult, ReferenceVisionRequest
from cueflow.term_candidates import CandidateOccurrence


class FakeExtractor:
    provider = "fake-lexicon"
    model = "fake-model"

    def __init__(self, outcomes: list[object] | None = None) -> None:
        self.outcomes = outcomes or []
        self.calls: list[LexiconExtractionRequest] = []
        self.closed = False

    def extract(self, request: LexiconExtractionRequest) -> LexiconExtractionResult:
        self.calls.append(request)
        if self.outcomes:
            outcome = self.outcomes.pop(0)
            if isinstance(outcome, BaseException):
                raise outcome
            if isinstance(outcome, LexiconExtractionResult):
                return outcome
        unit = request.units[0]
        text = str(unit["text"])
        term = "CueFlow"
        start = text.index(term)
        return _result(
            CandidateOccurrence(
                raw_surface_form=term,
                field_path=tuple(unit["field_path"]),
                start_offset=start,
                end_offset=start + len(term),
                category="proper_noun",
                proper_noun_subtype="product_brand_model_software",
                suggested_surface_form="CueFlow",
            )
        )

    def close(self) -> None:
        self.closed = True


class PreflightFailureExtractor(FakeExtractor):
    def preflight(self) -> None:
        raise RuntimeError("preflight unavailable")


class FakeVision:
    provider = "fake-reference"
    model = "fake-vision"

    def __init__(self, outcomes: list[ReferenceModelResult | Exception]) -> None:
        self.outcomes = outcomes

    def recognize(self, _request: ReferenceVisionRequest) -> ReferenceModelResult:
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    def close(self) -> None:
        return None


def _result(*occurrences: CandidateOccurrence) -> LexiconExtractionResult:
    return LexiconExtractionResult(
        occurrences=occurrences,
        response_id="lex-response",
        provider_usage={"total_tokens": 3},
        provider_cost=None,
    )


def _vision_result(text: str) -> ReferenceModelResult:
    return ReferenceModelResult(
        text=text,
        segments=(),
        response_id="vision-response",
        provider_usage=None,
        provider_usage_duration=None,
        provider_cost=None,
    )


def _extract_text_reference(
    context: ProjectContext,
    path: Path,
    extractor: FakeExtractor,
) -> dict[str, Any]:
    reference = register_reference_asset(context, path)
    return extract_reference(
        context,
        reference["reference_asset_id"],
        lexicon_factory=lambda: extractor,
    )


def test_reference_extraction_automatically_discovers_incremental_suggestions(
    tmp_path: Path,
) -> None:
    context = initialize_project(tmp_path / "project", "Lexicon")
    reference_path = tmp_path / "notes.txt"
    reference_path.write_text("Use CueFlow for captions.", encoding="utf-8")
    extractor = FakeExtractor()
    try:
        effective_before = context.current_artifact("effective_glossary").artifact_id
        first = _extract_text_reference(context, reference_path, extractor)
        assert first["suggestions"]["outcome"] == "complete"
        assert len(extractor.calls) == 1
        suggestion = list_suggestions(context)[0]
        assert suggestion["display_term"] == "CueFlow"
        assert suggestion["proper_noun_subtype"] == "product_brand_model_software"

        lexicon_run = context.registry.lexicon_run(first["suggestions"]["run_id"])
        assert lexicon_run["kind"] == "lexicon"
        item = context.registry.lexicon_work_items_for_run(lexicon_run["run_id"])[0]
        candidate_set = context.artifact(str(item["candidate_set_artifact_id"]))
        occurrence = candidate_set.payload["candidates"][0]["occurrences"][0]
        assert occurrence["raw_surface_form"] == "CueFlow"
        assert occurrence["field_path"] == ["content", "blocks", 0, "text"]
        assert candidate_set.producer.provider == "fake-lexicon"

        same_bundle = context.artifact(first["bundle_artifact_id"])
        skipped = discover_terms_for_bundle(
            context, same_bundle, extractor_factory=lambda: extractor
        )
        assert skipped["status"] == "no_new_evidence"
        assert len(extractor.calls) == 1

        reference_path.write_text("CueFlow appears in new Evidence.", encoding="utf-8")
        second = extract_reference(
            context,
            str(context.registry.reference_runs()[0]["reference_asset_id"]),
            lexicon_factory=lambda: extractor,
        )
        assert second["suggestions"]["outcome"] == "complete"
        assert len(extractor.calls) == 2
        assert list_suggestions(context)[0]["occurrence_count"] == 2

        assert context.current_artifact("effective_glossary").artifact_id == effective_before
        assert project_status(context)["latest_source_run"] is None
    finally:
        context.close()


def test_empty_result_succeeds_and_invalid_evidence_location_fails(tmp_path: Path) -> None:
    context = ProjectContext.create(tmp_path / "project", "Validation")
    first_path = tmp_path / "empty.txt"
    first_path.write_text("No extracted terms.", encoding="utf-8")
    empty = FakeExtractor([_result()])
    try:
        first = _extract_text_reference(context, first_path, empty)
        assert first["suggestions"]["outcome"] == "complete"
        assert list_suggestions(context) == []

        second_path = tmp_path / "invalid.txt"
        second_path.write_text("CueFlow", encoding="utf-8")
        invalid = FakeExtractor(
            [
                _result(
                    CandidateOccurrence(
                        raw_surface_form="invented",
                        field_path=("content", "blocks", 0, "text"),
                        start_offset=0,
                        end_offset=8,
                        category="noun_or_term",
                    )
                )
            ]
        )
        second = _extract_text_reference(context, second_path, invalid)
        assert second["outcome"] == "complete"
        assert second["suggestions"]["outcome"] == "failed"
        work_item = context.registry.lexicon_work_items_for_run(
            second["suggestions"]["run_id"]
        )[0]
        assert work_item["status"] == "failed"
        assert work_item["failure_code"] == "ContractError"
    finally:
        context.close()


def test_extractor_initialization_failure_is_finalized_without_masking_reference(
    tmp_path: Path,
) -> None:
    context = ProjectContext.create(tmp_path / "project", "Initialization")
    path = tmp_path / "initialization.txt"
    path.write_text("CueFlow", encoding="utf-8")

    def fail_factory() -> FakeExtractor:
        raise RuntimeError("cannot initialize provider")

    reference = register_reference_asset(context, path)
    try:
        result = extract_reference(
            context,
            reference["reference_asset_id"],
            lexicon_factory=fail_factory,
        )
        assert result["outcome"] == "complete"
        assert result["suggestions"]["outcome"] == "failed"
        run = context.registry.lexicon_run(result["suggestions"]["run_id"])
        assert run["status"] == "failed"
        assert context.registry.lexicon_work_items_for_run(run["run_id"])[0][
            "failure_code"
        ] == "RuntimeError"
    finally:
        context.close()


def test_sent_attempt_limit_counts_ambiguous_but_not_preflight(tmp_path: Path) -> None:
    context = ProjectContext.create(tmp_path / "project", "Attempts")
    path = tmp_path / "attempts.txt"
    path.write_text("CueFlow", encoding="utf-8")
    preflight = PreflightFailureExtractor()
    try:
        first = _extract_text_reference(context, path, preflight)
        work_item_id = str(
            context.registry.lexicon_work_items_for_run(first["suggestions"]["run_id"])[0][
                "work_item_id"
            ]
        )
        assert context.registry.sent_lexicon_attempt_count(work_item_id) == 0

        ambiguous = FakeExtractor([DeliveryAmbiguousError("unknown delivery")])
        with pytest.raises(LexiconRunFailedError):
            retry_suggestion_work_item(
                context, work_item_id, extractor_factory=lambda: ambiguous
            )
        assert context.registry.sent_lexicon_attempt_count(work_item_id) == 1

        explicit = FakeExtractor([RuntimeError("provider failed")])
        with pytest.raises(LexiconRunFailedError):
            retry_suggestion_work_item(context, work_item_id, extractor_factory=lambda: explicit)
        assert context.registry.sent_lexicon_attempt_count(work_item_id) == 2
        with pytest.raises(ContractError, match="sent-attempt limit exhausted"):
            retry_suggestion_work_item(
                context, work_item_id, extractor_factory=lambda: FakeExtractor()
            )
    finally:
        context.close()


def test_partial_reference_discovers_successes_then_retry_only_new_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    context = ProjectContext.create(tmp_path / "project", "Partial")
    path = tmp_path / "reference.png"
    path.write_bytes(bytes.fromhex("89504E470D0A1A0A") + b"fixture")
    reference = register_reference_asset(context, path)
    monkeypatch.setattr(
        reference_orchestrator,
        "_plan_work",
        lambda *_args, **_kwargs: (
            ReferenceWorkSpec("image_visual", "image_visual", "image_vision", {}),
            ReferenceWorkSpec("burned_subtitle", "burned_subtitle", "image_vision", {}),
        ),
    )

    def convert(_path: Path, _runtime: object, output: Path) -> tuple[Path, tuple[str, ...], str]:
        output.write_bytes(b"converted")
        return output, ("fake-convert",), "sha256:" + "1" * 64

    monkeypatch.setattr(reference_orchestrator, "prepare_visual_image", convert)
    vision = FakeVision([_vision_result("CueFlow first"), RuntimeError("second failed")])
    extractor = FakeExtractor()
    try:
        with pytest.raises(ReferenceRunFailedError) as failed:
            extract_reference(
                context,
                reference["reference_asset_id"],
                providers=ReferenceProviders(vision=vision),
                lexicon_factory=lambda: extractor,
            )
        run_id = failed.value.run_id
        assert failed.value.outcome == "partial"
        assert len(extractor.calls) == 1
        first_evidence_id = extractor.calls[0].evidence_artifact_id

        failed_item = next(
            row
            for row in context.registry.reference_work_items_for_run(run_id)
            if row["status"] == "failed"
        )
        vision.outcomes.append(_vision_result("CueFlow retry"))
        retried = retry_reference_work_item(
            context,
            str(failed_item["work_item_id"]),
            providers=ReferenceProviders(vision=vision),
            lexicon_factory=lambda: extractor,
        )
        assert retried["outcome"] == "complete"
        assert len(extractor.calls) == 2
        assert extractor.calls[0].evidence_artifact_id == first_evidence_id
        assert extractor.calls[1].evidence_artifact_id != first_evidence_id
        assert list_suggestions(context)[0]["occurrence_count"] == 2
    finally:
        context.close()


def test_lexicon_recovery_is_isolated_from_source_and_reference_runs(tmp_path: Path) -> None:
    context = ProjectContext.create(tmp_path / "project", "Recovery")
    path = tmp_path / "recovery.txt"
    path.write_text("CueFlow", encoding="utf-8")
    extractor = FakeExtractor()
    try:
        extracted = _extract_text_reference(context, path, extractor)
        lexicon_run_id = extracted["suggestions"]["run_id"]
        reference_run_id = extracted["run_id"]
        source_run_id = context.registry.create_source_run(
            context.project_id, {"source_asset_id": "fixture"}, "sha256:" + "0" * 64
        )
        context.registry.set_run_status(source_run_id, "running")
        context.registry.set_run_status(reference_run_id, "running")
        context.registry.set_run_status(lexicon_run_id, "running")

        assert context.registry.recover_running_lexicon_runs() == [lexicon_run_id]
        assert context.registry.run(lexicon_run_id)["status"] == "interrupted"
        assert context.registry.run(reference_run_id)["status"] == "running"
        assert context.registry.run(source_run_id)["status"] == "running"
    finally:
        context.close()
