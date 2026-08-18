from __future__ import annotations

import json
import subprocess
import sys

import pytest

from cueflow.atomizer import build_transcript_payload
from cueflow.canonical import artifact_content_hash, canonical_bytes
from cueflow.config import CLOUD_MODEL, LOCAL_ALIGNER_REPO, PROFILES
from cueflow.errors import ContractError
from cueflow.schema import ArtifactEnvelope, InputRef, Producer


def producer() -> Producer:
    return Producer(
        component="cueflow.test",
        component_version="0.1.0",
        processing_profile="LOCAL_PROFILE",
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
        schema_version="1.0.0",
        producer=producer().as_dict(),
        inputs=inputs,
        payload=payload,
    )
    changed = dict(payload)
    changed["atomizer_version"] = "0.1.1"
    assert original != artifact_content_hash(
        artifact_kind="transcript",
        scope_key="chunk_0001",
        schema_version="1.0.0",
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


def test_envelope_roundtrip_and_unknown_major_rejected() -> None:
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
    assert ArtifactEnvelope.from_dict(json.loads(json.dumps(envelope.as_dict()))) == envelope
    invalid = envelope.as_dict()
    invalid["schema_version"] = "2.0.0"
    with pytest.raises(ContractError, match="unsupported schema major"):
        ArtifactEnvelope.from_dict(invalid)


def test_profiles_fix_models_and_keep_alignment_local() -> None:
    assert set(PROFILES) == {"LOCAL_PROFILE", "CLOUD_PROFILE"}
    assert PROFILES["CLOUD_PROFILE"].semantic_model == CLOUD_MODEL
    assert all(profile.aligner_model == LOCAL_ALIGNER_REPO for profile in PROFILES.values())
    assert all(profile.aligner_provider == "qwen-local" for profile in PROFILES.values())
