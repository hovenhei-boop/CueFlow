from __future__ import annotations

from pathlib import Path

import pytest

from cueflow.errors import ContractError, SuppressionConflictError
from cueflow.lexicon import (
    add_entry,
    delete_entry,
    edit_entry,
    ingest_candidate_occurrences,
    list_blacklist,
    list_entries,
    list_suggestions,
    list_trash,
    remove_blacklist,
    restore_trash,
    review_candidate,
    set_entry_enabled,
    set_trash_retention,
)
from cueflow.orchestrator import initialize_project
from cueflow.project import ProjectContext
from cueflow.term_candidates import CandidateOccurrence, EvidenceUnit, validate_occurrence


def _occurrence(
    term: str,
    *,
    category: str = "noun_or_term",
    subtype: str | None = None,
    prefix: str = "before ",
) -> object:
    text = prefix + term + " after"
    return validate_occurrence(
        CandidateOccurrence(
            raw_surface_form=term,
            field_path=("content",),
            start_offset=len(prefix),
            end_offset=len(prefix) + len(term),
            category=category,
            proper_noun_subtype=subtype,
            suggested_surface_form=term.upper(),
            risk_tags=("model_spelling",),
        ),
        (EvidenceUnit(("content",), text, {"page_number": 2}),),
    )


def _ingest(context: ProjectContext, evidence_id: str, *values: object) -> list[dict[str, object]]:
    return ingest_candidate_occurrences(
        context,
        evidence_artifact_id=evidence_id,
        reference_role="document_text",
        occurrences=values,  # type: ignore[arg-type]
    )


def test_candidate_identity_provenance_categories_and_sorting(tmp_path: Path) -> None:
    context = ProjectContext.create(tmp_path / "project", "Candidates")
    try:
        _ingest(
            context,
            "evidence-1",
            _occurrence(
                " 顾华玺 ", category="proper_noun", subtype="person", prefix="x"
            ),
            _occurrence("long terminology"),
            _occurrence("Run", category="verb"),
            _occurrence("cueflow"),
            _occurrence("CueFlow"),
            _occurrence("Cue-Flow"),
        )
        suggestions = list_suggestions(context)
        assert [row["display_category"] for row in suggestions] == [
            "proper_noun",
            "noun_or_term",
            "noun_or_term",
            "noun_or_term",
            "noun_or_term",
            "verb",
        ]
        noun_terms = [
            row["normalized_surface_form"]
            for row in suggestions
            if row["display_category"] == "noun_or_term"
        ]
        assert noun_terms[0] == "long terminology"
        assert {"CueFlow", "cueflow", "Cue-Flow"}.issubset(noun_terms)
        assert len({row["candidate_id"] for row in suggestions}) == 6

        occurrence = context.registry._connection.execute(
            "SELECT * FROM term_occurrences WHERE raw_surface_form=' 顾华玺 '"
        ).fetchone()
        assert occurrence is not None
        assert occurrence["start_offset"] == 1
        assert occurrence["end_offset"] == 6
        assert occurrence["suggested_surface_form"] == "顾华玺"
        assert '"page_number": 2' in occurrence["coordinates_json"]
    finally:
        context.close()


def test_review_trash_blacklist_and_explicit_conflict_choices(tmp_path: Path) -> None:
    context = initialize_project(tmp_path / "project", "Review")
    try:
        effective_before = context.current_artifact("effective_glossary").artifact_id
        _ingest(context, "e1", _occurrence("RejectMe"), _occurrence("BlockMe"))
        by_term = {row["display_term"]: row for row in list_suggestions(context)}

        rejected = review_candidate(
            context,
            by_term["RejectMe"]["candidate_id"],
            "reject",
            expected_revision=by_term["RejectMe"]["revision"],
        )
        assert rejected["status"] == "rejected"
        trash_id = list_trash(context)[0]["trash_id"]
        assert _ingest(context, "e2", _occurrence("RejectMe"))[0]["disposition"] == (
            "suppressed_trash"
        )
        assert restore_trash(context, trash_id)["status"] == "restored"
        assert [row["display_term"] for row in list_suggestions(context)] == [
            "RejectMe",
            "BlockMe",
        ]

        block = next(row for row in list_suggestions(context) if row["display_term"] == "BlockMe")
        review_candidate(
            context,
            block["candidate_id"],
            "blacklist",
            expected_revision=block["revision"],
        )
        assert _ingest(context, "e3", _occurrence("BlockMe"))[0]["disposition"] == (
            "suppressed_blacklist"
        )
        blacklist_id = list_blacklist(context)[0]["blacklist_id"]

        with pytest.raises(SuppressionConflictError) as conflict:
            add_entry(context, "BlockMe", category="noun_or_term")
        assert conflict.value.conflicts == ("blacklist",)
        assert (
            add_entry(
                context,
                "BlockMe",
                category="noun_or_term",
                suppression_policy="cancel",
            )["status"]
            == "cancelled"
        )
        kept = add_entry(
            context,
            "BlockMe",
            category="noun_or_term",
            suppression_policy="keep_and_add",
        )
        assert kept["status"] == "added"
        assert len(list_blacklist(context)) == 1
        assert _ingest(context, "e4", _occurrence("BlockMe"))[0]["disposition"] == (
            "already_in_project_lexicon"
        )
        remove_blacklist(context, blacklist_id)
        assert list_blacklist(context) == []
        assert context.current_artifact("effective_glossary").artifact_id == effective_before
    finally:
        context.close()


def test_project_lexicon_edit_disable_delete_restore_and_retention(tmp_path: Path) -> None:
    context = ProjectContext.create(tmp_path / "project", "Entries")
    try:
        added = add_entry(
            context,
            "Qwen ASR",
            category="proper_noun",
            proper_noun_subtype="product_brand_model_software",
        )
        entry_id = added["entry_id"]
        entry = list_entries(context)[0]
        disabled = set_entry_enabled(
            context, entry_id, enabled=False, expected_revision=entry["revision"]
        )
        assert disabled["status"] == "disabled"
        entry = list_entries(context)[0]
        updated = edit_entry(
            context,
            entry_id,
            term="Qwen-ASR",
            category="proper_noun",
            proper_noun_subtype="product_brand_model_software",
            expected_revision=entry["revision"],
        )
        assert updated["status"] == "updated"
        entry = list_entries(context)[0]
        assert entry["term"] == "Qwen-ASR"
        assert entry["enabled"] == 0

        deleted = delete_entry(context, entry_id, expected_revision=entry["revision"])
        assert list_entries(context) == []
        assert restore_trash(context, deleted["trash_id"])["status"] == "restored"
        restored = list_entries(context)[0]
        assert restored["term"] == "Qwen-ASR"
        assert restored["enabled"] == 0

        assert set_trash_retention(context, 15) == {"trash_retention_days": 15}
        assert set_trash_retention(context, None) == {"trash_retention_days": None}
        with pytest.raises(ContractError, match="trash retention"):
            set_trash_retention(context, 7)

        revisions = context.registry._connection.execute(
            "SELECT ordinal FROM project_lexicon_revisions ORDER BY ordinal"
        ).fetchall()
        assert [row[0] for row in revisions] == [1, 2, 3, 4, 5]
        assert context.current_artifact("project_lexicon").payload["entries"][0][
            "enabled"
        ] is False
    finally:
        context.close()


def test_editing_an_accepted_entry_allows_old_raw_form_to_be_suggested_again(
    tmp_path: Path,
) -> None:
    context = ProjectContext.create(tmp_path / "project", "Edit candidate")
    try:
        _ingest(context, "e1", _occurrence("Qwen ASR"))
        candidate = list_suggestions(context)[0]
        accepted = review_candidate(
            context,
            candidate["candidate_id"],
            "accept",
            expected_revision=candidate["revision"],
        )
        entry = list_entries(context)[0]
        edit_entry(
            context,
            accepted["entry_id"],
            term="Qwen-ASR",
            category="noun_or_term",
            proper_noun_subtype=None,
            expected_revision=entry["revision"],
        )

        observation = _ingest(context, "e2", _occurrence("Qwen ASR"))[0]
        assert observation["disposition"] == "suggested"
        assert list_suggestions(context)[0]["display_term"] == "Qwen ASR"
    finally:
        context.close()
