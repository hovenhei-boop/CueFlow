from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import asdict

import pytest

from cueflow.atomizer import build_transcript_payload
from cueflow.canonical import artifact_content_hash, canonical_bytes
from cueflow.config import (
    SCHEMA_VERSION,
    SEMANTIC_RETRY_RESET_LIMIT,
    QaRulesetConfig,
)
from cueflow.errors import ContractError
from cueflow.registry import DDL
from cueflow.schema import (
    ARTIFACT_KINDS,
    ArtifactEnvelope,
    InputRef,
    Producer,
    validate_project_lexicon_payload,
    validate_term_candidate_set_payload,
)


def producer() -> Producer:
    return Producer(
        component="cueflow.test",
        component_version="0.1.0",
        provider="fake",
        model="fake",
        config_hash="sha256:" + "0" * 64,
    )


def test_rfc8785_golden_and_cross_process() -> None:
    value = {"z": "世界", "a": [1, True, None], "nested": {"b": 2, "a": 1}}
    expected = b'{"a":[1,true,null],"nested":{"a":1,"b":2},"z":"\xe4\xb8\x96\xe7\x95\x8c"}'
    assert canonical_bytes(value) == expected
    code = (
        "from cueflow.canonical import canonical_bytes; "
        f"print(canonical_bytes({value!r}).hex())"
    )
    output = subprocess.check_output([sys.executable, "-c", code], text=True).strip()
    assert bytes.fromhex(output) == expected


def test_hash_changes_when_atomizer_version_changes() -> None:
    payload = build_transcript_payload(
        chunk_id="chunk_0001", source_text="Hello, 世界！", language="Chinese"
    )
    inputs = [InputRef(role="media_chunk", artifact_id="art_input").as_dict()]
    original = artifact_content_hash(
        artifact_kind="transcript",
        scope_key="chunk_0001",
        schema_version=SCHEMA_VERSION,
        producer=producer().as_dict(),
        inputs=inputs,
        payload=payload,
    )
    changed = dict(payload)
    changed["atomizer_version"] = "0.1.1"
    assert original != artifact_content_hash(
        artifact_kind="transcript",
        scope_key="chunk_0001",
        schema_version=SCHEMA_VERSION,
        producer=producer().as_dict(),
        inputs=inputs,
        payload=changed,
    )


def test_transcript_rebuild_and_punctuation_is_decoration() -> None:
    payload = build_transcript_payload(
        chunk_id="chunk_0001", source_text="Hello, 世界！", language="Chinese"
    )
    assert [atom["text"] for atom in payload["atoms"]] == ["Hello", "世", "界"]
    assert payload["atoms"][0]["decoration_after"] == ", "
    assert payload["atoms"][-1]["decoration_after"] == "！"


def test_atomizer_keeps_internal_apostrophe_and_hyphen_inside_word() -> None:
    payload = build_transcript_payload(
        chunk_id="chunk_0001",
        source_text="wasn't Heriot-Watt",
        language="English",
    )
    assert [atom["text"] for atom in payload["atoms"]] == ["wasn't", "Heriot-Watt"]


@pytest.mark.parametrize("version", ["0.0.0", "999.0.0"])
def test_envelope_roundtrip_and_incompatible_schema_rejected(version: str) -> None:
    payload = build_transcript_payload(
        chunk_id="chunk_0001", source_text="测试", language="Chinese"
    )
    envelope = ArtifactEnvelope.create(
        artifact_kind="transcript",
        scope_key="chunk_0001",
        producer=producer(),
        inputs=(InputRef(role="media_chunk", artifact_id="art_input"),),
        payload=payload,
    )
    assert envelope.schema_version == SCHEMA_VERSION == "4.0.0"
    assert ArtifactEnvelope.from_dict(json.loads(json.dumps(envelope.as_dict()))) == envelope
    invalid = envelope.as_dict()
    invalid["schema_version"] = version
    with pytest.raises(ContractError, match="incompatible schema version"):
        ArtifactEnvelope.from_dict(invalid)

    invalid = envelope.as_dict()
    invalid["producer"]["unknown_field"] = None
    with pytest.raises(ContractError, match="producer fields"):
        ArtifactEnvelope.from_dict(invalid)

    invalid = envelope.as_dict()
    del invalid["producer"]["model"]
    with pytest.raises(ContractError, match="producer fields"):
        ArtifactEnvelope.from_dict(invalid)


def test_artifact_kind_allowlist_is_the_complete_current_pipeline() -> None:
    assert ARTIFACT_KINDS == frozenset(
        {
            "media_probe",
            "timeline_audio",
            "chunk_plan",
            "media_chunk",
            "system_glossary",
            "project_glossary",
            "effective_glossary",
            "transcript",
            "alignment",
            "subtitle",
            "qa",
            "srt_render",
            "reference_input",
            "reference_evidence",
            "reference_bundle",
            "lexicon_input",
            "term_candidate_set",
            "project_lexicon",
        }
    )


def test_semantic_retry_reset_limit_is_a_fixed_data_model_constant() -> None:
    assert SEMANTIC_RETRY_RESET_LIMIT == 2
    assert "semantic_retry_reset_limit" not in asdict(QaRulesetConfig())
    assert (
        f"semantic_budget_window BETWEEN 0 AND {SEMANTIC_RETRY_RESET_LIMIT}" in DDL
    )
    assert f"window_index BETWEEN 1 AND {SEMANTIC_RETRY_RESET_LIMIT}" in DDL


def test_lexicon_artifact_payloads_validate_provenance_and_exact_identity() -> None:
    candidate_payload = {
        "run_id": "run_1",
        "work_item_id": "lwi_1",
        "evidence_artifact_id": "art_evidence",
        "candidates": [
            {
                "candidate_id": "cand_1",
                "normalized_surface_form": "CueFlow",
                "display_term": "CueFlow",
                "display_category": "proper_noun",
                "display_proper_noun_subtype": "product_brand_model_software",
                "disposition": "suggested",
                "occurrences": [
                    {
                        "raw_surface_form": "CueFlow",
                        "suggested_surface_form": None,
                        "field_path": ["content", "blocks", 0, "text"],
                        "start_offset": 4,
                        "end_offset": 11,
                        "category": "proper_noun",
                        "proper_noun_subtype": "product_brand_model_software",
                        "risk_tags": [],
                        "context_before": "Use ",
                        "context_after": ".",
                        "coordinates": {"page_number": 1},
                    }
                ],
            }
        ],
    }
    validate_term_candidate_set_payload(candidate_payload)
    candidate_payload["candidates"][0]["candidate_id"] = None
    candidate_payload["candidates"][0]["disposition"] = "not_a_disposition"
    with pytest.raises(ContractError, match="invalid disposition"):
        validate_term_candidate_set_payload(candidate_payload)
    candidate_payload["candidates"][0]["candidate_id"] = "cand_1"
    candidate_payload["candidates"][0]["disposition"] = "suggested"
    candidate_payload["candidates"][0]["occurrences"][0]["end_offset"] = 4
    with pytest.raises(ContractError, match="interval"):
        validate_term_candidate_set_payload(candidate_payload)

    project_payload = {
        "revision_id": "lexrev_1",
        "ordinal": 1,
        "parent_revision_id": None,
        "decision_id": "dec_1",
        "entries": [
            {
                "entry_id": "lex_1",
                "term": "é",
                "category": "noun_or_term",
                "proper_noun_subtype": None,
                "enabled": True,
                "entry_revision": 1,
            },
            {
                "entry_id": "lex_2",
                "term": "e\u0301",
                "category": "noun_or_term",
                "proper_noun_subtype": None,
                "enabled": True,
                "entry_revision": 1,
            },
        ],
    }
    with pytest.raises(ContractError, match="unique"):
        validate_project_lexicon_payload(project_payload)
