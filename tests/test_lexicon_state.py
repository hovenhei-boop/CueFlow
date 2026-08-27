from __future__ import annotations

from pathlib import Path

import pytest

from cueflow.errors import ContractError, SuppressionConflictError
from cueflow.lexicon import (
    add_blacklist,
    add_entry,
    block_entry,
    edit_entry,
    ingest_candidate_occurrences,
    list_blacklist,
    list_entries,
    list_suggestions,
    remove_entry,
    review_candidate,
    set_entry_enabled,
    unblock_blacklist,
    update_blacklist,
)
from cueflow.orchestrator import initialize_project
from cueflow.project import ProjectContext
from cueflow.schema import utc_now
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


def test_candidate_dismiss_temporary_permanent_and_expiry(tmp_path: Path) -> None:
    context = initialize_project(tmp_path / "project", "Review")
    try:
        effective_before = context.current_artifact("effective_glossary").artifact_id
        _ingest(
            context,
            "e1",
            _occurrence("DismissMe"),
            _occurrence("Temporary"),
            _occurrence("Permanent"),
        )
        by_term = {row["display_term"]: row for row in list_suggestions(context)}

        dismissed = review_candidate(
            context,
            by_term["DismissMe"]["candidate_id"],
            "dismiss",
            expected_revision=by_term["DismissMe"]["revision"],
        )
        assert dismissed["status"] == "dismissed"
        assert _ingest(context, "e2", _occurrence("DismissMe"))[0]["disposition"] == (
            "suggested"
        )

        temporary = by_term["Temporary"]
        blocked = review_candidate(
            context,
            temporary["candidate_id"],
            "block_temporary",
            blacklist_days=15,
            expected_revision=temporary["revision"],
        )
        assert blocked["kind"] == "temporary"
        assert blocked["expires_at"] is not None
        assert _ingest(context, "e3", _occurrence("Temporary"))[0]["disposition"] == (
            "suppressed_blacklist"
        )
        context.registry._connection.execute(
            "UPDATE lexicon_blacklist SET expires_at=? WHERE blacklist_id=?",
            (utc_now(), blocked["blacklist_id"]),
        )
        context.registry._connection.commit()
        assert _ingest(context, "e4", _occurrence("Temporary"))[0]["disposition"] == (
            "suggested"
        )

        permanent = by_term["Permanent"]
        permanent_block = review_candidate(
            context,
            permanent["candidate_id"],
            "block_permanent",
            expected_revision=permanent["revision"],
        )
        assert permanent_block["expires_at"] is None
        assert _ingest(context, "e5", _occurrence("Permanent"))[0]["disposition"] == (
            "suppressed_blacklist"
        )
        assert unblock_blacklist(
            context,
            permanent_block["blacklist_id"],
            expected_revision=permanent_block["revision"],
        )["status"] == "unblocked"
        assert _ingest(context, "e6", _occurrence("Permanent"))[0]["disposition"] == (
            "suggested"
        )
        assert context.current_artifact("effective_glossary").artifact_id == effective_before
    finally:
        context.close()


def test_entry_remove_block_and_blacklist_exclusivity(tmp_path: Path) -> None:
    context = initialize_project(tmp_path / "project", "Entries")
    other = initialize_project(tmp_path / "other", "Other")
    try:
        _ingest(context, "e1", _occurrence("RemoveMe"))
        candidate = list_suggestions(context)[0]
        accepted = review_candidate(
            context,
            candidate["candidate_id"],
            "accept",
            expected_revision=candidate["revision"],
        )
        entry = list_entries(context)[0]
        assert remove_entry(
            context, accepted["entry_id"], expected_revision=entry["revision"]
        )["status"] == "removed"
        assert _ingest(context, "e2", _occurrence("RemoveMe"))[0]["disposition"] == (
            "suggested"
        )

        added = add_entry(context, "BlockMe", category="noun_or_term")
        block_entry_row = next(
            row for row in list_entries(context) if row["entry_id"] == added["entry_id"]
        )
        blocked = block_entry(
            context,
            added["entry_id"],
            kind="temporary",
            days=30,
            expected_revision=block_entry_row["revision"],
        )
        assert blocked["status"] == "blacklisted"
        assert _ingest(context, "e3", _occurrence("BlockMe"))[0]["disposition"] == (
            "suppressed_blacklist"
        )

        manual = add_blacklist(
            context, "Conflict", kind="permanent", days=None
        )
        with pytest.raises(SuppressionConflictError) as conflict:
            add_entry(context, "Conflict", category="noun_or_term")
        assert conflict.value.conflicts == ("blacklist",)
        assert add_entry(
            context,
            "Conflict",
            category="noun_or_term",
            blacklist_policy="cancel",
        )["status"] == "cancelled"
        added_conflict = add_entry(
            context,
            "Conflict",
            category="noun_or_term",
            blacklist_policy="unblock_and_add",
        )
        assert added_conflict["status"] == "added"
        assert all(row["blacklist_id"] != manual["blacklist_id"] for row in list_blacklist(context))
        with pytest.raises(ContractError, match="Project Lexicon already"):
            add_blacklist(context, "Conflict", kind="permanent", days=None)

        overlap = context.registry._connection.execute(
            """
            SELECT COUNT(*) FROM project_lexicon_entries e
            JOIN lexicon_blacklist b
              ON b.normalization_version=e.normalization_version
             AND b.normalized_surface_form=e.normalized_surface_form
            WHERE e.status='active'
            """
        ).fetchone()
        assert overlap is not None and overlap[0] == 0

        assert _ingest(other, "other-e1", _occurrence("BlockMe"))[0]["disposition"] == (
            "suggested"
        )
    finally:
        context.close()
        other.close()


def test_blacklist_update_fixed_durations_and_revision(tmp_path: Path) -> None:
    context = ProjectContext.create(tmp_path / "project", "Blacklist")
    try:
        with pytest.raises(ContractError, match="must be one of"):
            add_blacklist(context, "Invalid", kind="temporary", days=7)
        temporary = add_blacklist(context, "Mutable", kind="temporary", days=15)
        with pytest.raises(ContractError, match="revision conflict"):
            update_blacklist(
                context,
                temporary["blacklist_id"],
                kind="temporary",
                days=30,
                expected_revision=99,
            )
        updated = update_blacklist(
            context,
            temporary["blacklist_id"],
            kind="temporary",
            days=30,
            expected_revision=temporary["revision"],
        )
        assert updated["revision"] == 2
        permanent = update_blacklist(
            context,
            temporary["blacklist_id"],
            kind="permanent",
            days=None,
            expected_revision=updated["revision"],
        )
        assert permanent["kind"] == "permanent"
        assert permanent["expires_at"] is None
        with pytest.raises(ContractError, match="can only be unblocked"):
            update_blacklist(
                context,
                temporary["blacklist_id"],
                kind="temporary",
                days=60,
                expected_revision=permanent["revision"],
            )
        assert list_blacklist(context, kind="temporary") == []
        assert len(list_blacklist(context, kind="permanent")) == 1
    finally:
        context.close()


def test_project_lexicon_edit_disable_and_remove(tmp_path: Path) -> None:
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

        removed = remove_entry(context, entry_id, expected_revision=entry["revision"])
        assert removed["status"] == "removed"
        assert list_entries(context) == []
        removed_entry = list_entries(context, include_removed=True)[0]
        assert removed_entry["term"] == "Qwen-ASR"
        assert removed_entry["enabled"] == 0
        assert removed_entry["status"] == "removed"

        revisions = context.registry._connection.execute(
            "SELECT ordinal FROM project_lexicon_revisions ORDER BY ordinal"
        ).fetchall()
        assert [row[0] for row in revisions] == [1, 2, 3, 4]
        assert context.current_artifact("project_lexicon").payload["entries"] == []
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
