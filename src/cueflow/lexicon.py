from __future__ import annotations

import json
import sqlite3
import uuid
from collections import defaultdict
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta, timezone
from typing import Any, Literal, cast

from cueflow.canonical import hash_json
from cueflow.config import COMPONENT_VERSION
from cueflow.errors import ContractError, IntegrityError, SuppressionConflictError
from cueflow.project import ProjectContext
from cueflow.schema import ArtifactEnvelope, InputRef, Producer, utc_now
from cueflow.term_candidates import (
    LEXICON_NORMALIZATION_VERSION,
    ValidatedOccurrence,
    candidate_sort_key,
    normalize_surface_form,
    preferred_category,
    validate_category,
)

BLACKLIST_DURATION_DAYS = (15, 30, 60, 120)
BlacklistKind = Literal["temporary", "permanent"]
BlacklistPolicy = Literal["prompt", "unblock_and_add", "cancel"]
CandidateAction = Literal[
    "accept",
    "edit_accept",
    "dismiss",
    "block_temporary",
    "block_permanent",
]


def ingest_candidate_occurrences(
    context: ProjectContext,
    *,
    evidence_artifact_id: str,
    reference_role: str,
    occurrences: Sequence[ValidatedOccurrence],
) -> list[dict[str, Any]]:
    grouped: dict[str, list[ValidatedOccurrence]] = defaultdict(list)
    for occurrence in occurrences:
        grouped[occurrence.normalized_surface_form].append(occurrence)
    observations: list[dict[str, Any]] = []
    with context.registry.transaction() as connection:
        _expire_temporary_blacklist(connection)
        for normalized in sorted(grouped, key=lambda value: value.encode("utf-8")):
            values = grouped[normalized]
            active_entry = connection.execute(
                """
                SELECT entry_id FROM project_lexicon_entries
                WHERE normalization_version=? AND normalized_surface_form=?
                  AND status='active'
                """,
                (LEXICON_NORMALIZATION_VERSION, normalized),
            ).fetchone()
            if active_entry is not None:
                disposition = "already_in_project_lexicon"
                candidate_id = None
            elif _has_blacklist(connection, normalized):
                disposition = "suppressed_blacklist"
                candidate_id = None
            else:
                candidate_id = _upsert_candidate(connection, normalized, values)
                disposition = "suggested"
                for value in values:
                    _insert_occurrence(
                        connection,
                        candidate_id=candidate_id,
                        evidence_artifact_id=evidence_artifact_id,
                        reference_role=reference_role,
                        occurrence=value,
                    )
            display_category, display_subtype = _preferred_from_occurrences(values)
            observations.append(
                {
                    "candidate_id": candidate_id,
                    "normalized_surface_form": normalized,
                    "display_term": values[0].raw_surface_form,
                    "display_category": display_category,
                    "display_proper_noun_subtype": display_subtype,
                    "disposition": disposition,
                    "occurrences": [_occurrence_payload(value) for value in values],
                }
            )
    return observations


def list_suggestions(context: ProjectContext) -> list[dict[str, Any]]:
    with context.registry.transaction() as connection:
        _expire_temporary_blacklist(connection)
        rows = connection.execute(
            """
            SELECT c.*, COUNT(o.occurrence_id) AS occurrence_count
            FROM term_candidates c
            LEFT JOIN term_occurrences o ON o.candidate_id=c.candidate_id
            WHERE c.status='pending'
            GROUP BY c.candidate_id
            """
        ).fetchall()
    result = [dict(row) for row in rows]
    result.sort(key=candidate_sort_key)
    return result


def review_candidate(
    context: ProjectContext,
    candidate_id: str,
    action: CandidateAction,
    *,
    edited_term: str | None = None,
    edited_category: str | None = None,
    edited_subtype: str | None = None,
    expected_revision: int,
    blacklist_days: int | None = None,
    blacklist_policy: BlacklistPolicy = "prompt",
) -> dict[str, Any]:
    if action not in {
        "accept",
        "edit_accept",
        "dismiss",
        "block_temporary",
        "block_permanent",
    }:
        raise ContractError("invalid candidate action")
    block_kind = _candidate_block_kind(action)
    _validate_blacklist_duration(block_kind, blacklist_days)
    with context.registry.transaction() as connection:
        _expire_temporary_blacklist(connection)
        candidate = _candidate(connection, candidate_id)
        if candidate["status"] != "pending":
            raise ContractError("only a pending Suggested Term can be reviewed")
        if int(candidate["revision"]) != expected_revision:
            raise ContractError("candidate revision conflict")
        if action == "dismiss":
            decision_id = _record_decision(
                connection, candidate_id, "dismiss", {"revision": expected_revision}
            )
            connection.execute(
                "UPDATE term_candidates SET status='dismissed', revision=revision+1, "
                "updated_at=? WHERE candidate_id=?",
                (utc_now(), candidate_id),
            )
            return {"status": "dismissed", "decision_id": decision_id}
        if block_kind is not None:
            normalized = str(candidate["normalized_surface_form"])
            blacklist_id = _insert_blacklist(
                connection,
                str(candidate["display_term"]),
                normalized,
                kind=block_kind,
                days=blacklist_days,
            )
            decision_id = _record_decision(
                connection,
                candidate_id,
                "block_" + block_kind,
                {
                    "revision": expected_revision,
                    "blacklist_id": blacklist_id,
                    "days": blacklist_days,
                },
            )
            connection.execute(
                "UPDATE term_candidates SET status='blacklisted', revision=revision+1, "
                "updated_at=? WHERE candidate_id=?",
                (utc_now(), candidate_id),
            )
            blacklist = _blacklist(connection, blacklist_id)
            return {
                "status": "blacklisted",
                "blacklist_id": blacklist_id,
                "kind": blacklist["kind"],
                "expires_at": blacklist["expires_at"],
                "revision": blacklist["revision"],
                "decision_id": decision_id,
            }

        if action == "accept" and edited_term is not None:
            raise ContractError("accept does not take edited_term; use edit_accept")
        target_term = (
            str(candidate["display_term"]) if action == "accept" else edited_term
        )
        if target_term is None:
            raise ContractError("edit_accept requires edited_term")
        normalized = normalize_surface_form(target_term)
        category = edited_category or str(candidate["display_category"])
        subtype = (
            edited_subtype
            if edited_category is not None or edited_subtype is not None
            else cast(str | None, candidate["proper_noun_subtype"])
        )
        category, subtype = validate_category(category, subtype)
        if not _resolve_blacklist_conflict(connection, normalized, blacklist_policy):
            return {"status": "cancelled"}
        _ensure_no_active_entry(connection, normalized)
        decision_id = _record_decision(
            connection,
            candidate_id,
            action,
            {
                "revision": expected_revision,
                "term": target_term,
                "category": category,
                "proper_noun_subtype": subtype,
                "blacklist_policy": blacklist_policy,
            },
        )
        entry_id = _insert_entry(
            connection,
            term=target_term,
            normalized=normalized,
            category=category,
            subtype=subtype,
            source_candidate_id=candidate_id,
        )
        candidate_status = (
            "accepted"
            if normalized == str(candidate["normalized_surface_form"])
            else "edited_accepted"
        )
        connection.execute(
            "UPDATE term_candidates SET status=?, revision=revision+1, updated_at=? "
            "WHERE candidate_id=?",
            (candidate_status, utc_now(), candidate_id),
        )
        revision = _publish_project_lexicon_revision(context, connection, decision_id)
        return {
            "status": candidate_status,
            "entry_id": entry_id,
            "decision_id": decision_id,
            "project_lexicon_revision": revision,
        }


def add_entry(
    context: ProjectContext,
    term: str,
    *,
    category: str,
    proper_noun_subtype: str | None = None,
    blacklist_policy: BlacklistPolicy = "prompt",
) -> dict[str, Any]:
    normalized = normalize_surface_form(term)
    category, proper_noun_subtype = validate_category(category, proper_noun_subtype)
    with context.registry.transaction() as connection:
        _expire_temporary_blacklist(connection)
        if not _resolve_blacklist_conflict(connection, normalized, blacklist_policy):
            return {"status": "cancelled"}
        _ensure_no_active_entry(connection, normalized)
        decision_id = _record_decision(
            connection,
            None,
            "manual_add",
            {
                "term": term,
                "category": category,
                "proper_noun_subtype": proper_noun_subtype,
                "blacklist_policy": blacklist_policy,
            },
        )
        entry_id = _insert_entry(
            connection,
            term=term,
            normalized=normalized,
            category=category,
            subtype=proper_noun_subtype,
            source_candidate_id=None,
        )
        revision = _publish_project_lexicon_revision(context, connection, decision_id)
        return {"status": "added", "entry_id": entry_id, "revision": revision}


def edit_entry(
    context: ProjectContext,
    entry_id: str,
    *,
    term: str,
    category: str,
    proper_noun_subtype: str | None,
    expected_revision: int,
    blacklist_policy: BlacklistPolicy = "prompt",
) -> dict[str, Any]:
    normalized = normalize_surface_form(term)
    category, proper_noun_subtype = validate_category(category, proper_noun_subtype)
    with context.registry.transaction() as connection:
        _expire_temporary_blacklist(connection)
        entry = _entry(connection, entry_id)
        _require_active_entry_revision(entry, expected_revision)
        if normalized != str(entry["normalized_surface_form"]):
            if not _resolve_blacklist_conflict(connection, normalized, blacklist_policy):
                return {"status": "cancelled"}
            _ensure_no_active_entry(connection, normalized, excluding_entry_id=entry_id)
        decision_id = _record_decision(
            connection,
            cast(str | None, entry["source_candidate_id"]),
            "entry_edit",
            {
                "entry_id": entry_id,
                "term": term,
                "category": category,
                "proper_noun_subtype": proper_noun_subtype,
                "expected_revision": expected_revision,
                "blacklist_policy": blacklist_policy,
            },
        )
        connection.execute(
            """
            UPDATE project_lexicon_entries
            SET term=?, normalization_version=?, normalized_surface_form=?, category=?,
                proper_noun_subtype=?, revision=revision+1, updated_at=?
            WHERE entry_id=?
            """,
            (
                term,
                LEXICON_NORMALIZATION_VERSION,
                normalized,
                category,
                proper_noun_subtype,
                utc_now(),
                entry_id,
            ),
        )
        if (
            normalized != str(entry["normalized_surface_form"])
            and entry["source_candidate_id"] is not None
        ):
            connection.execute(
                "UPDATE term_candidates SET status='edited_accepted', "
                "revision=revision+1, updated_at=? WHERE candidate_id=?",
                (utc_now(), entry["source_candidate_id"]),
            )
        revision = _publish_project_lexicon_revision(context, connection, decision_id)
        return {"status": "updated", "entry_id": entry_id, "revision": revision}


def set_entry_enabled(
    context: ProjectContext, entry_id: str, *, enabled: bool, expected_revision: int
) -> dict[str, Any]:
    with context.registry.transaction() as connection:
        entry = _entry(connection, entry_id)
        _require_active_entry_revision(entry, expected_revision)
        decision_id = _record_decision(
            connection,
            cast(str | None, entry["source_candidate_id"]),
            "entry_enable" if enabled else "entry_disable",
            {"entry_id": entry_id, "expected_revision": expected_revision},
        )
        connection.execute(
            "UPDATE project_lexicon_entries SET enabled=?, revision=revision+1, "
            "updated_at=? WHERE entry_id=?",
            (int(enabled), utc_now(), entry_id),
        )
        revision = _publish_project_lexicon_revision(context, connection, decision_id)
        return {"status": "enabled" if enabled else "disabled", "revision": revision}


def remove_entry(
    context: ProjectContext, entry_id: str, *, expected_revision: int
) -> dict[str, Any]:
    with context.registry.transaction() as connection:
        entry = _entry(connection, entry_id)
        _require_active_entry_revision(entry, expected_revision)
        decision_id = _record_decision(
            connection,
            cast(str | None, entry["source_candidate_id"]),
            "entry_remove",
            {"entry_id": entry_id, "expected_revision": expected_revision},
        )
        connection.execute(
            "UPDATE project_lexicon_entries SET status='removed', revision=revision+1, "
            "updated_at=? WHERE entry_id=?",
            (utc_now(), entry_id),
        )
        if entry["source_candidate_id"] is not None:
            connection.execute(
                "UPDATE term_candidates SET status='dismissed', revision=revision+1, "
                "updated_at=? WHERE candidate_id=?",
                (utc_now(), entry["source_candidate_id"]),
            )
        revision = _publish_project_lexicon_revision(context, connection, decision_id)
        return {
            "status": "removed",
            "entry_id": entry_id,
            "decision_id": decision_id,
            "revision": revision,
        }


def block_entry(
    context: ProjectContext,
    entry_id: str,
    *,
    kind: BlacklistKind,
    days: int | None,
    expected_revision: int,
) -> dict[str, Any]:
    _validate_blacklist_duration(kind, days)
    with context.registry.transaction() as connection:
        _expire_temporary_blacklist(connection)
        entry = _entry(connection, entry_id)
        _require_active_entry_revision(entry, expected_revision)
        blacklist_id = _insert_blacklist(
            connection,
            str(entry["term"]),
            str(entry["normalized_surface_form"]),
            kind=kind,
            days=days,
        )
        decision_id = _record_decision(
            connection,
            cast(str | None, entry["source_candidate_id"]),
            "entry_block_" + kind,
            {
                "entry_id": entry_id,
                "expected_revision": expected_revision,
                "blacklist_id": blacklist_id,
                "days": days,
            },
        )
        connection.execute(
            "UPDATE project_lexicon_entries SET status='removed', revision=revision+1, "
            "updated_at=? WHERE entry_id=?",
            (utc_now(), entry_id),
        )
        if entry["source_candidate_id"] is not None:
            connection.execute(
                "UPDATE term_candidates SET status='blacklisted', revision=revision+1, "
                "updated_at=? WHERE candidate_id=?",
                (utc_now(), entry["source_candidate_id"]),
            )
        revision = _publish_project_lexicon_revision(context, connection, decision_id)
        blacklist = _blacklist(connection, blacklist_id)
        return {
            "status": "blacklisted",
            "entry_id": entry_id,
            "blacklist_id": blacklist_id,
            "kind": blacklist["kind"],
            "expires_at": blacklist["expires_at"],
            "blacklist_revision": blacklist["revision"],
            "decision_id": decision_id,
            "project_lexicon_revision": revision,
        }


def list_entries(context: ProjectContext, *, include_removed: bool = False) -> list[dict[str, Any]]:
    clause = "" if include_removed else " WHERE status='active'"
    rows = context.registry._connection.execute(  # noqa: SLF001 - Registry owns this schema
        "SELECT * FROM project_lexicon_entries" + clause
    ).fetchall()
    result = [dict(row) for row in rows]
    result.sort(
        key=lambda row: (
            candidate_sort_key(
                {
                    "display_category": row["category"],
                    "normalized_surface_form": row["normalized_surface_form"],
                    "candidate_id": row["entry_id"],
                }
            )
        )
    )
    return result


def add_blacklist(
    context: ProjectContext,
    surface_form: str,
    *,
    kind: BlacklistKind,
    days: int | None,
) -> dict[str, Any]:
    _validate_blacklist_duration(kind, days)
    normalized = normalize_surface_form(surface_form)
    with context.registry.transaction() as connection:
        _expire_temporary_blacklist(connection)
        _ensure_no_active_entry(connection, normalized)
        blacklist_id = _insert_blacklist(
            connection, surface_form, normalized, kind=kind, days=days
        )
        candidate_id = _candidate_id_for_normalized(connection, normalized)
        if candidate_id is not None:
            connection.execute(
                "UPDATE term_candidates SET status='blacklisted', revision=revision+1, "
                "updated_at=? WHERE candidate_id=?",
                (utc_now(), candidate_id),
            )
        decision_id = _record_decision(
            connection,
            candidate_id,
            "blacklist_add_" + kind,
            {
                "surface_form": surface_form,
                "blacklist_id": blacklist_id,
                "days": days,
            },
        )
        blacklist = _blacklist(connection, blacklist_id)
        return {
            "status": "blacklisted",
            "blacklist_id": blacklist_id,
            "kind": blacklist["kind"],
            "expires_at": blacklist["expires_at"],
            "revision": blacklist["revision"],
            "decision_id": decision_id,
        }


def update_blacklist(
    context: ProjectContext,
    blacklist_id: str,
    *,
    kind: BlacklistKind,
    days: int | None,
    expected_revision: int,
) -> dict[str, Any]:
    _validate_blacklist_duration(kind, days)
    with context.registry.transaction() as connection:
        _expire_temporary_blacklist(connection)
        row = _blacklist(connection, blacklist_id)
        if int(row["revision"]) != expected_revision:
            raise ContractError("Project Blacklist revision conflict")
        if row["kind"] != "temporary":
            raise ContractError("Permanent Project Blacklist items can only be unblocked")
        expires_at = _blacklist_expiration(kind, days)
        connection.execute(
            "UPDATE lexicon_blacklist SET kind=?, expires_at=?, revision=revision+1, "
            "updated_at=? WHERE blacklist_id=?",
            (kind, expires_at, utc_now(), blacklist_id),
        )
        candidate_id = _candidate_id_for_normalized(
            connection, str(row["normalized_surface_form"])
        )
        decision_id = _record_decision(
            connection,
            candidate_id,
            "blacklist_update_" + kind,
            {
                "blacklist_id": blacklist_id,
                "expected_revision": expected_revision,
                "days": days,
            },
        )
        updated = _blacklist(connection, blacklist_id)
        return {
            "status": "updated",
            "blacklist_id": blacklist_id,
            "kind": updated["kind"],
            "expires_at": updated["expires_at"],
            "revision": updated["revision"],
            "decision_id": decision_id,
        }


def unblock_blacklist(
    context: ProjectContext, blacklist_id: str, *, expected_revision: int
) -> dict[str, Any]:
    with context.registry.transaction() as connection:
        _expire_temporary_blacklist(connection)
        row = _blacklist(connection, blacklist_id)
        if int(row["revision"]) != expected_revision:
            raise ContractError("Project Blacklist revision conflict")
        candidate_id = _candidate_id_for_normalized(
            connection, str(row["normalized_surface_form"])
        )
        decision_id = _record_decision(
            connection,
            candidate_id,
            "blacklist_unblock",
            {
                "blacklist_id": blacklist_id,
                "expected_revision": expected_revision,
                "surface_form": row["surface_form"],
            },
        )
        connection.execute(
            "DELETE FROM lexicon_blacklist WHERE blacklist_id=?", (blacklist_id,)
        )
        return {
            "status": "unblocked",
            "blacklist_id": blacklist_id,
            "decision_id": decision_id,
        }


def list_blacklist(
    context: ProjectContext, *, kind: Literal["all", "temporary", "permanent"] = "all"
) -> list[dict[str, Any]]:
    if kind not in {"all", "temporary", "permanent"}:
        raise ContractError("invalid Project Blacklist kind")
    with context.registry.transaction() as connection:
        _expire_temporary_blacklist(connection)
        clause = "" if kind == "all" else " WHERE kind=?"
        parameters: tuple[str, ...] = () if kind == "all" else (kind,)
        rows = connection.execute(
            "SELECT * FROM lexicon_blacklist"
            + clause
            + " ORDER BY kind DESC, normalized_surface_form COLLATE BINARY",
            parameters,
        ).fetchall()
    return [dict(row) for row in rows]


def _upsert_candidate(
    connection: sqlite3.Connection,
    normalized: str,
    occurrences: Sequence[ValidatedOccurrence],
) -> str:
    category, subtype = _preferred_from_occurrences(occurrences)
    row = connection.execute(
        "SELECT * FROM term_candidates WHERE normalization_version=? "
        "AND normalized_surface_form=?",
        (LEXICON_NORMALIZATION_VERSION, normalized),
    ).fetchone()
    now = utc_now()
    if row is None:
        candidate_id = "cand_" + uuid.uuid4().hex
        connection.execute(
            """
            INSERT INTO term_candidates
            (candidate_id, normalization_version, normalized_surface_form, display_term,
             display_category, proper_noun_subtype, status, revision, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, 'pending', 1, ?, ?)
            """,
            (
                candidate_id,
                LEXICON_NORMALIZATION_VERSION,
                normalized,
                occurrences[0].raw_surface_form,
                category,
                subtype,
                now,
                now,
            ),
        )
        return candidate_id
    candidate_id = str(row["candidate_id"])
    display_category, display_subtype = preferred_category(
        str(row["display_category"]),
        cast(str | None, row["proper_noun_subtype"]),
        category,
        subtype,
    )
    status = str(row["status"])
    if status in {"accepted", "dismissed", "blacklisted", "edited_accepted"}:
        status = "pending"
    connection.execute(
        """
        UPDATE term_candidates
        SET display_category=?, proper_noun_subtype=?, status=?, revision=revision+1,
            updated_at=? WHERE candidate_id=?
        """,
        (display_category, display_subtype, status, now, candidate_id),
    )
    return candidate_id


def _insert_occurrence(
    connection: sqlite3.Connection,
    *,
    candidate_id: str,
    evidence_artifact_id: str,
    reference_role: str,
    occurrence: ValidatedOccurrence,
) -> None:
    connection.execute(
        """
        INSERT OR IGNORE INTO term_occurrences
        (occurrence_id, candidate_id, evidence_artifact_id, reference_role,
         raw_surface_form, suggested_surface_form, proposed_category,
         proper_noun_subtype, risk_tags_json, field_path_json, start_offset,
         end_offset, context_before, context_after, coordinates_json, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "occ_" + uuid.uuid4().hex,
            candidate_id,
            evidence_artifact_id,
            reference_role,
            occurrence.raw_surface_form,
            occurrence.suggested_surface_form,
            occurrence.category,
            occurrence.proper_noun_subtype,
            json.dumps(list(occurrence.risk_tags), ensure_ascii=False, sort_keys=True),
            json.dumps(list(occurrence.field_path), ensure_ascii=False, separators=(",", ":")),
            occurrence.start_offset,
            occurrence.end_offset,
            occurrence.context_before,
            occurrence.context_after,
            json.dumps(occurrence.coordinates, ensure_ascii=False, sort_keys=True)
            if occurrence.coordinates
            else None,
            utc_now(),
        ),
    )


def _preferred_from_occurrences(
    occurrences: Sequence[ValidatedOccurrence],
) -> tuple[str, str | None]:
    if not occurrences:
        raise ContractError("candidate aggregation requires at least one occurrence")
    category = occurrences[0].category
    subtype = occurrences[0].proper_noun_subtype
    for occurrence in occurrences[1:]:
        category, subtype = preferred_category(
            category, subtype, occurrence.category, occurrence.proper_noun_subtype
        )
    return category, subtype


def _occurrence_payload(value: ValidatedOccurrence) -> dict[str, Any]:
    return {
        "raw_surface_form": value.raw_surface_form,
        "suggested_surface_form": value.suggested_surface_form,
        "field_path": list(value.field_path),
        "start_offset": value.start_offset,
        "end_offset": value.end_offset,
        "category": value.category,
        "proper_noun_subtype": value.proper_noun_subtype,
        "risk_tags": list(value.risk_tags),
        "context_before": value.context_before,
        "context_after": value.context_after,
        "coordinates": dict(value.coordinates),
    }


def _candidate(connection: sqlite3.Connection, candidate_id: str) -> sqlite3.Row:
    row = connection.execute(
        "SELECT * FROM term_candidates WHERE candidate_id=?", (candidate_id,)
    ).fetchone()
    if row is None:
        raise IntegrityError(f"unknown Suggested Term: {candidate_id}")
    return cast(sqlite3.Row, row)


def _entry(connection: sqlite3.Connection, entry_id: str) -> sqlite3.Row:
    row = connection.execute(
        "SELECT * FROM project_lexicon_entries WHERE entry_id=?", (entry_id,)
    ).fetchone()
    if row is None:
        raise IntegrityError(f"unknown Project Lexicon entry: {entry_id}")
    return cast(sqlite3.Row, row)


def _require_active_entry_revision(entry: sqlite3.Row, expected_revision: int) -> None:
    if entry["status"] != "active":
        raise ContractError("Project Lexicon entry is removed")
    if int(entry["revision"]) != expected_revision:
        raise ContractError("Project Lexicon entry revision conflict")


def _ensure_no_active_entry(
    connection: sqlite3.Connection,
    normalized: str,
    *,
    excluding_entry_id: str | None = None,
) -> None:
    query = (
        "SELECT entry_id FROM project_lexicon_entries "
        "WHERE normalization_version=? AND normalized_surface_form=? AND status='active'"
    )
    parameters: list[Any] = [LEXICON_NORMALIZATION_VERSION, normalized]
    if excluding_entry_id is not None:
        query += " AND entry_id<>?"
        parameters.append(excluding_entry_id)
    if connection.execute(query, parameters).fetchone() is not None:
        raise ContractError("Project Lexicon already contains the exact term")


def _insert_entry(
    connection: sqlite3.Connection,
    *,
    term: str,
    normalized: str,
    category: str,
    subtype: str | None,
    source_candidate_id: str | None,
) -> str:
    entry_id = "lex_" + uuid.uuid4().hex
    now = utc_now()
    connection.execute(
        """
        INSERT INTO project_lexicon_entries
        (entry_id, term, normalization_version, normalized_surface_form, category,
         proper_noun_subtype, source_candidate_id, enabled, status, revision,
         created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, 1, 'active', 1, ?, ?)
        """,
        (
            entry_id,
            term,
            LEXICON_NORMALIZATION_VERSION,
            normalized,
            category,
            subtype,
            source_candidate_id,
            now,
            now,
        ),
    )
    return entry_id


def _record_decision(
    connection: sqlite3.Connection,
    candidate_id: str | None,
    action: str,
    payload: Mapping[str, Any],
) -> str:
    decision_id = "dec_" + uuid.uuid4().hex
    connection.execute(
        """
        INSERT INTO candidate_decisions
        (decision_id, candidate_id, action, payload_json, created_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (
            decision_id,
            candidate_id,
            action,
            json.dumps(payload, ensure_ascii=False, sort_keys=True),
            utc_now(),
        ),
    )
    return decision_id


def _insert_blacklist(
    connection: sqlite3.Connection,
    surface_form: str,
    normalized: str,
    *,
    kind: BlacklistKind,
    days: int | None,
) -> str:
    _validate_blacklist_duration(kind, days)
    existing = connection.execute(
        "SELECT blacklist_id FROM lexicon_blacklist WHERE normalization_version=? "
        "AND normalized_surface_form=?",
        (LEXICON_NORMALIZATION_VERSION, normalized),
    ).fetchone()
    if existing is not None:
        raise ContractError("Project Blacklist already contains the exact term")
    blacklist_id = "black_" + uuid.uuid4().hex
    now = utc_now()
    connection.execute(
        """
        INSERT INTO lexicon_blacklist
        (blacklist_id, normalization_version, normalized_surface_form,
         surface_form, kind, expires_at, revision, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?)
        """,
        (
            blacklist_id,
            LEXICON_NORMALIZATION_VERSION,
            normalized,
            surface_form,
            kind,
            _blacklist_expiration(kind, days),
            now,
            now,
        ),
    )
    return blacklist_id


def _blacklist(connection: sqlite3.Connection, blacklist_id: str) -> sqlite3.Row:
    row = connection.execute(
        "SELECT * FROM lexicon_blacklist WHERE blacklist_id=?", (blacklist_id,)
    ).fetchone()
    if row is None:
        raise IntegrityError(f"unknown active Project Blacklist item: {blacklist_id}")
    return cast(sqlite3.Row, row)


def _candidate_id_for_normalized(
    connection: sqlite3.Connection, normalized: str
) -> str | None:
    row = connection.execute(
        "SELECT candidate_id FROM term_candidates WHERE normalization_version=? "
        "AND normalized_surface_form=?",
        (LEXICON_NORMALIZATION_VERSION, normalized),
    ).fetchone()
    return None if row is None else str(row["candidate_id"])


def _has_blacklist(connection: sqlite3.Connection, normalized: str) -> bool:
    return (
        connection.execute(
            "SELECT 1 FROM lexicon_blacklist WHERE normalization_version=? "
            "AND normalized_surface_form=?",
            (LEXICON_NORMALIZATION_VERSION, normalized),
        ).fetchone()
        is not None
    )


def _resolve_blacklist_conflict(
    connection: sqlite3.Connection,
    normalized: str,
    policy: BlacklistPolicy,
) -> bool:
    if policy not in {"prompt", "unblock_and_add", "cancel"}:
        raise ContractError("invalid Project Blacklist conflict policy")
    row = connection.execute(
        "SELECT * FROM lexicon_blacklist WHERE normalization_version=? "
        "AND normalized_surface_form=?",
        (LEXICON_NORMALIZATION_VERSION, normalized),
    ).fetchone()
    if row is None:
        return True
    if policy == "prompt":
        raise SuppressionConflictError(normalized, ("blacklist",))
    if policy == "cancel":
        return False
    candidate_id = _candidate_id_for_normalized(connection, normalized)
    _record_decision(
        connection,
        candidate_id,
        "blacklist_unblock_for_add",
        {
            "blacklist_id": row["blacklist_id"],
            "surface_form": row["surface_form"],
        },
    )
    connection.execute(
        "DELETE FROM lexicon_blacklist WHERE blacklist_id=?",
        (row["blacklist_id"],),
    )
    return True


def _candidate_block_kind(action: CandidateAction) -> BlacklistKind | None:
    if action == "block_temporary":
        return "temporary"
    if action == "block_permanent":
        return "permanent"
    return None


def _validate_blacklist_duration(kind: BlacklistKind | None, days: int | None) -> None:
    if kind is None:
        if days is not None:
            raise ContractError("blacklist days are only valid for a temporary block")
        return
    if kind == "temporary":
        if days not in BLACKLIST_DURATION_DAYS:
            allowed = ", ".join(str(value) for value in BLACKLIST_DURATION_DAYS)
            raise ContractError(f"temporary Project Blacklist days must be one of {allowed}")
        return
    if kind == "permanent":
        if days is not None:
            raise ContractError("permanent Project Blacklist items cannot have days")
        return
    raise ContractError("invalid Project Blacklist kind")


def _blacklist_expiration(kind: BlacklistKind, days: int | None) -> str | None:
    _validate_blacklist_duration(kind, days)
    if kind == "permanent":
        return None
    assert days is not None
    return (
        datetime.now(timezone.utc) + timedelta(days=days)
    ).isoformat().replace("+00:00", "Z")


def _expire_temporary_blacklist(connection: sqlite3.Connection) -> int:
    now = utc_now()
    rows = connection.execute(
        "SELECT * FROM lexicon_blacklist WHERE kind='temporary' AND expires_at<=?",
        (now,),
    ).fetchall()
    for row in rows:
        candidate_id = _candidate_id_for_normalized(
            connection, str(row["normalized_surface_form"])
        )
        _record_decision(
            connection,
            candidate_id,
            "blacklist_expire",
            {
                "blacklist_id": row["blacklist_id"],
                "surface_form": row["surface_form"],
                "expires_at": row["expires_at"],
            },
        )
    connection.execute(
        "DELETE FROM lexicon_blacklist WHERE kind='temporary' AND expires_at<=?",
        (now,),
    )
    return len(rows)


def _publish_project_lexicon_revision(
    context: ProjectContext,
    connection: sqlite3.Connection,
    decision_id: str,
) -> dict[str, Any]:
    previous = connection.execute(
        "SELECT * FROM project_lexicon_revisions ORDER BY ordinal DESC LIMIT 1"
    ).fetchone()
    ordinal = 1 if previous is None else int(previous["ordinal"]) + 1
    parent_id = None if previous is None else str(previous["revision_id"])
    revision_id = "lexrev_" + uuid.uuid4().hex
    rows = connection.execute(
        "SELECT * FROM project_lexicon_entries WHERE status='active'"
    ).fetchall()
    entries = [
        {
            "entry_id": str(row["entry_id"]),
            "term": str(row["term"]),
            "category": str(row["category"]),
            "proper_noun_subtype": row["proper_noun_subtype"],
            "enabled": bool(row["enabled"]),
            "entry_revision": int(row["revision"]),
        }
        for row in rows
    ]
    entries.sort(
        key=lambda row: candidate_sort_key(
            {
                "display_category": row["category"],
                "normalized_surface_form": normalize_surface_form(str(row["term"])),
                "candidate_id": row["entry_id"],
            }
        )
    )
    inputs: list[InputRef] = []
    if previous is not None:
        inputs.append(
            InputRef(role="previous_project_lexicon", artifact_id=str(previous["artifact_id"]))
        )
    envelope = ArtifactEnvelope.create(
        artifact_kind="project_lexicon",
        scope_key="global",
        producer=Producer(
            component="project-lexicon",
            component_version=COMPONENT_VERSION,
            provider=None,
            model=None,
            config_hash=hash_json(
                {
                    "normalization_version": LEXICON_NORMALIZATION_VERSION,
                    "ordering": "category,length_desc,utf8,candidate_id",
                }
            ),
        ),
        inputs=inputs,
        payload={
            "revision_id": revision_id,
            "ordinal": ordinal,
            "parent_revision_id": parent_id,
            "decision_id": decision_id,
            "entries": entries,
        },
    )
    context.publisher.publish(envelope)
    connection.execute(
        """
        INSERT INTO project_lexicon_revisions
        (revision_id, ordinal, parent_revision_id, artifact_id, decision_id, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (revision_id, ordinal, parent_id, envelope.artifact_id, decision_id, utc_now()),
    )
    return {
        "revision_id": revision_id,
        "ordinal": ordinal,
        "artifact_id": envelope.artifact_id,
    }
