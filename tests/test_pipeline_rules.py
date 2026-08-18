from __future__ import annotations

from typing import Any

import pytest

from cueflow.alignment import build_alignment_payload
from cueflow.atomizer import build_transcript_payload
from cueflow.canonical import hash_json
from cueflow.errors import ContractError, ExportBlockedError
from cueflow.export import validate_export_gate
from cueflow.filler import local_filler_review_payload
from cueflow.providers import AlignmentToken, parse_cloud_filler_response
from cueflow.qa import evaluate_semantic_attempts, glossary_single_atom_conflicts
from cueflow.schema import ArtifactEnvelope, InputRef, Producer
from cueflow.segmentation import segment_subtitles


def _producer(component: str) -> Producer:
    return Producer(component, "0.1.0", None, None, None, hash_json({"component": component}))


def _aligned_chunk(
    text: str,
    *,
    chunk_id: str = "chunk_0001",
    chunk_start: int = 1_000,
) -> tuple[ArtifactEnvelope, ArtifactEnvelope, ArtifactEnvelope]:
    transcript_payload = build_transcript_payload(
        chunk_id=chunk_id,
        source_text=text,
        language="Chinese",
    )
    media_chunk = ArtifactEnvelope.create(
        artifact_kind="media_chunk",
        scope_key=chunk_id,
        producer=_producer("chunker"),
        inputs=[InputRef(role="timeline_audio", artifact_id="art_" + "1" * 64)],
        payload={
            "chunk_id": chunk_id,
            "ordinal": 0,
            "global_start_ms": chunk_start,
            "global_end_ms": chunk_start + 10_000,
            "timeline_audio_artifact_id": "art_" + "1" * 64,
        },
    )
    transcript = ArtifactEnvelope.create(
        artifact_kind="transcript",
        scope_key=chunk_id,
        producer=_producer("semantic"),
        inputs=[InputRef(role="media_chunk", artifact_id=media_chunk.artifact_id)],
        payload=transcript_payload,
    )
    tokens = [
        AlignmentToken(str(atom["text"]), index * 100, (index + 1) * 100)
        for index, atom in enumerate(transcript_payload["atoms"])
    ]
    alignment_payload = build_alignment_payload(
        media_chunk_artifact_id=media_chunk.artifact_id,
        media_chunk=media_chunk.payload,
        transcript_artifact_id=transcript.artifact_id,
        transcript=transcript.payload,
        tokens=tokens,
    )
    alignment = ArtifactEnvelope.create(
        artifact_kind="alignment",
        scope_key=chunk_id,
        producer=_producer("alignment"),
        inputs=[
            InputRef(role="media_chunk", artifact_id=media_chunk.artifact_id),
            InputRef(role="transcript", artifact_id=transcript.artifact_id),
        ],
        payload=alignment_payload,
    )
    return media_chunk, transcript, alignment


def _attempt(text: str) -> dict[str, Any]:
    return build_transcript_payload(chunk_id="chunk_0001", source_text=text, language="Chinese")


def test_alignment_applies_chunk_offset_exactly_once_and_rejects_token_mismatch() -> None:
    chunk, transcript, alignment = _aligned_chunk("你好")
    assert alignment.payload["assignments"][0]["global_start_ms"] == 1_000
    assert alignment.payload["assignments"][1]["global_end_ms"] == 1_200
    mismatched = build_alignment_payload(
        media_chunk_artifact_id=chunk.artifact_id,
        media_chunk=chunk.payload,
        transcript_artifact_id=transcript.artifact_id,
        transcript=transcript.payload,
        tokens=[AlignmentToken("错", 0, 100), AlignmentToken("好", 100, 200)],
    )
    assert [item["status"] for item in mismatched["assignments"]] == ["unaligned", "unaligned"]
    mismatched_chunk = dict(chunk.payload)
    mismatched_chunk["chunk_id"] = "chunk_0002"
    with pytest.raises(ContractError, match="mismatched"):
        build_alignment_payload(
            media_chunk_artifact_id=chunk.artifact_id,
            media_chunk=mismatched_chunk,
            transcript_artifact_id=transcript.artifact_id,
            transcript=transcript.payload,
            tokens=[],
        )


def test_segmenter_limits_units_styles_punctuation_and_preserves_long_protected_term() -> None:
    _, transcript, alignment = _aligned_chunk("一二三四五六七八九十甲乙。")
    subtitle, warnings = segment_subtitles(
        [transcript], [alignment], [], duration_ms=11_000
    )
    assert [cue["display_unit_count"] for cue in subtitle["cues"]] == [10, 2]
    assert subtitle["cues"][-1]["text"] == "甲乙"
    assert warnings == []

    protected_text = "一二三四五六七八九十甲。"
    _, protected_transcript, protected_alignment = _aligned_chunk(protected_text)
    protected_subtitle, protected_warnings = segment_subtitles(
        [protected_transcript],
        [protected_alignment],
        [protected_text.rstrip("。")],
        duration_ms=11_000,
    )
    assert protected_subtitle["cues"][0]["display_unit_count"] == 11
    assert protected_subtitle["cues"][0]["protected_overflow"] is True
    assert protected_warnings[0]["code"] == "protected_unit_exceeds_display_limit"


def test_segmenter_prefers_clause_boundary_over_dangling_preposition() -> None:
    text = "He wasn't even that big when I started listening to him."
    _, transcript, alignment = _aligned_chunk(text)
    subtitle, _ = segment_subtitles([transcript], [alignment], [], duration_ms=11_000)
    assert [cue["text"] for cue in subtitle["cues"]] == [
        "He wasn't even that big",
        "when I started listening to him",
    ]


def test_subtitle_combines_multiple_chunk_alignments_without_global_alignment() -> None:
    _, first_transcript, first_alignment = _aligned_chunk(
        "你好，", chunk_id="chunk_0001", chunk_start=0
    )
    _, second_transcript, second_alignment = _aligned_chunk(
        "世界。", chunk_id="chunk_0002", chunk_start=2_000
    )
    subtitle, _ = segment_subtitles(
        [first_transcript, second_transcript],
        [first_alignment, second_alignment],
        [],
        duration_ms=12_000,
    )
    assert len(subtitle["cues"]) == 1
    assert [span["chunk_id"] for span in subtitle["cues"][0]["atom_spans"]] == [
        "chunk_0001",
        "chunk_0002",
    ]


def test_export_gate_enumerates_every_chunk_in_current_plan() -> None:
    _, transcript, alignment = _aligned_chunk("测试", chunk_start=0)
    chunk_plan = ArtifactEnvelope.create(
        artifact_kind="chunk_plan",
        scope_key="global",
        producer=_producer("chunker"),
        inputs=[],
        payload={
            "duration_ms": 10_000,
            "timeline_audio_artifact_id": "art_" + "1" * 64,
            "chunks": [
                {
                    "chunk_id": "chunk_0001",
                    "global_start_ms": 0,
                    "global_end_ms": 5_000,
                },
                {
                    "chunk_id": "chunk_0002",
                    "global_start_ms": 5_000,
                    "global_end_ms": 10_000,
                },
            ],
        },
    )
    subtitle_payload, _ = segment_subtitles(
        [transcript], [alignment], [], duration_ms=10_000
    )
    subtitle = ArtifactEnvelope.create(
        artifact_kind="subtitle",
        scope_key="global",
        producer=_producer("segmenter"),
        inputs=[
            InputRef(role="transcript", artifact_id=transcript.artifact_id),
            InputRef(role="alignment", artifact_id=alignment.artifact_id),
        ],
        payload=subtitle_payload,
    )
    qa = ArtifactEnvelope.create(
        artifact_kind="qa",
        scope_key="global",
        producer=_producer("qa"),
        inputs=[InputRef(role="subtitle", artifact_id=subtitle.artifact_id)],
        payload={
            "subject_artifact_ids": [subtitle.artifact_id],
            "qa_ruleset_version": "0.1.0",
            "result": "passed",
            "issues": [],
        },
    )
    filler = ArtifactEnvelope.create(
        artifact_kind="filler_review",
        scope_key="global",
        producer=_producer("filler"),
        inputs=[InputRef(role="subtitle", artifact_id=subtitle.artifact_id)],
        payload={
            "mode": "deterministic_local",
            "status": "completed",
            "candidates": [],
            "suppressions": [],
        },
    )

    class FakeRegistry:
        def current_pointer(self, project_id: str, kind: str, scope: str) -> dict[str, Any]:
            artifact = {
                "chunk_plan": chunk_plan,
                "subtitle": subtitle,
                "qa": qa,
                "filler_review": filler,
                "transcript": transcript,
                "alignment": alignment,
            }[kind]
            return {"artifact_id": artifact.artifact_id, "is_stale": 0}

    context = type(
        "FakeContext", (), {"registry": FakeRegistry(), "project_id": "project"}
    )()
    with pytest.raises(ExportBlockedError, match="not covered"):
        validate_export_gate(
            context,
            chunk_plan=chunk_plan,
            transcripts=[transcript],
            alignments=[alignment],
            subtitle=subtitle,
            qa=qa,
            filler_review=filler,
        )


def test_export_gate_allows_warnings_but_rejects_blocking_qa() -> None:
    media_chunk, transcript, alignment = _aligned_chunk("测试", chunk_start=0)
    chunk_plan = ArtifactEnvelope.create(
        artifact_kind="chunk_plan",
        scope_key="global",
        producer=_producer("chunker"),
        inputs=[],
        payload={
            "duration_ms": 10_000,
            "timeline_audio_artifact_id": "art_" + "1" * 64,
            "chunks": [
                {
                    "chunk_id": "chunk_0001",
                    "global_start_ms": 0,
                    "global_end_ms": 10_000,
                }
            ],
        },
    )
    subtitle_payload, _ = segment_subtitles(
        [transcript], [alignment], [], duration_ms=10_000
    )
    subtitle = ArtifactEnvelope.create(
        artifact_kind="subtitle",
        scope_key="global",
        producer=_producer("segmenter"),
        inputs=[
            InputRef(role="transcript", artifact_id=transcript.artifact_id),
            InputRef(role="alignment", artifact_id=alignment.artifact_id),
        ],
        payload=subtitle_payload,
    )

    def qa_artifact(result: str, severity: str) -> ArtifactEnvelope:
        return ArtifactEnvelope.create(
            artifact_kind="qa",
            scope_key="global",
            producer=_producer("qa"),
            inputs=[InputRef(role="subtitle", artifact_id=subtitle.artifact_id)],
            payload={
                "subject_artifact_ids": [subtitle.artifact_id],
                "qa_ruleset_version": "0.1.0",
                "result": result,
                "issues": [
                    {
                        "issue_id": "issue_00001",
                        "severity": severity,
                        "code": "fixture",
                        "resolution_status": "unresolved",
                    }
                ],
            },
        )

    def filler_artifact(qa: ArtifactEnvelope) -> ArtifactEnvelope:
        return ArtifactEnvelope.create(
            artifact_kind="filler_review",
            scope_key="global",
            producer=_producer("filler"),
            inputs=[
                InputRef(role="subtitle", artifact_id=subtitle.artifact_id),
                InputRef(role="qa", artifact_id=qa.artifact_id),
                InputRef(role="transcript", artifact_id=transcript.artifact_id),
                InputRef(role="alignment", artifact_id=alignment.artifact_id),
            ],
            payload={
                "subtitle_artifact_id": subtitle.artifact_id,
                "review_config_hash": "sha256:" + "2" * 64,
                "mode": "deterministic_local",
                "status": "completed",
                "candidates": [],
                "suppressions": [],
                "warnings": [],
            },
        )

    class FakeRegistry:
        def __init__(self, qa: ArtifactEnvelope, filler: ArtifactEnvelope) -> None:
            self.artifacts = {
                ("chunk_plan", "global"): chunk_plan,
                ("media_chunk", "chunk_0001"): media_chunk,
                ("transcript", "chunk_0001"): transcript,
                ("alignment", "chunk_0001"): alignment,
                ("subtitle", "global"): subtitle,
                ("qa", "global"): qa,
                ("filler_review", "global"): filler,
            }

        def current_pointer(self, project_id: str, kind: str, scope: str) -> dict[str, Any]:
            return {"artifact_id": self.artifacts[(kind, scope)].artifact_id, "is_stale": 0}

    warning_qa = qa_artifact("warnings", "warning")
    warning_filler = filler_artifact(warning_qa)
    warning_context = type(
        "FakeContext",
        (),
        {"registry": FakeRegistry(warning_qa, warning_filler), "project_id": "project"},
    )()
    validate_export_gate(
        warning_context,
        chunk_plan=chunk_plan,
        transcripts=[transcript],
        alignments=[alignment],
        subtitle=subtitle,
        qa=warning_qa,
        filler_review=warning_filler,
    )

    blocked_qa = qa_artifact("blocked", "blocking_error")
    blocked_filler = filler_artifact(blocked_qa)
    blocked_context = type(
        "FakeContext",
        (),
        {"registry": FakeRegistry(blocked_qa, blocked_filler), "project_id": "project"},
    )()
    with pytest.raises(ExportBlockedError, match="structural blocking"):
        validate_export_gate(
            blocked_context,
            chunk_plan=chunk_plan,
            transcripts=[transcript],
            alignments=[alignment],
            subtitle=subtitle,
            qa=blocked_qa,
            filler_review=blocked_filler,
        )


def test_glossary_conflict_stability_rules_and_single_atom_exclusion() -> None:
    assert glossary_single_atom_conflicts(_attempt("古老师"), ["顾"]) == []
    assert glossary_single_atom_conflicts(_attempt("顾 Hua"), ["顾华"]) == []
    assert glossary_single_atom_conflicts(_attempt("古花老师"), ["顾华"]) == []
    first = _attempt("顾华西老师")
    assert glossary_single_atom_conflicts(first, ["顾华玺"])[0]["differing_atom_offset"] == 2
    assert evaluate_semantic_attempts([first], ["顾华玺"]).action == "rework"

    stable_conflict = evaluate_semantic_attempts([first, _attempt("顾华西老师")], ["顾华玺"])
    assert stable_conflict.action == "accepted"
    assert stable_conflict.issues[0]["code"] == "stable_glossary_conflict"

    resolved = evaluate_semantic_attempts(
        [first, _attempt("顾华玺老师"), _attempt("顾华玺老师")], ["顾华玺"]
    )
    assert resolved.action == "accepted"
    assert resolved.issues[0]["resolution_status"] == "resolved"

    unstable = evaluate_semantic_attempts(
        [
            first,
            _attempt("顾华玺老师"),
            _attempt("顾华喜老师"),
            _attempt("顾华西老师"),
        ],
        ["顾华玺"],
    )
    assert unstable.action == "accepted"
    assert unstable.issues[0]["code"] == "unstable_glossary_conflict"

    absent_candidates = evaluate_semantic_attempts(
        [
            first,
            _attempt("完全不同"),
            _attempt("完全不同"),
            _attempt("还是不同"),
        ],
        ["顾华玺"],
    )
    assert absent_candidates.issues[0]["code"] == "unstable_glossary_conflict"

    exact_elsewhere = evaluate_semantic_attempts(
        [first, _attempt("顾华西老师顾华玺"), _attempt("顾华西老师顾华玺")],
        ["顾华玺"],
    )
    assert exact_elsewhere.issues[0]["code"] == "stable_glossary_conflict"


def test_local_filler_review_only_marks_atom_and_keeps_subtitle_envelope() -> None:
    _, transcript, alignment = _aligned_chunk("这个方案可以啊。")
    subtitle, _ = segment_subtitles([transcript], [alignment], [], duration_ms=11_000)
    cue_before = dict(subtitle["cues"][0])
    review = local_filler_review_payload("art_" + "2" * 64, subtitle, duration_ms=11_000)
    assert review["suppressions"] == [
        {
            "cue_id": "cue_00001",
            "transcript_artifact_id": transcript.artifact_id,
            "atom_id": "a0007",
            "text": "啊",
            "reason": "terminal_filler",
        }
    ]
    assert subtitle["cues"][0] == cue_before
    assert transcript.payload["source_text"] == "这个方案可以啊。"

    _, retained_transcript, retained_alignment = _aligned_chunk("这个方案呢。")
    retained_subtitle, _ = segment_subtitles(
        [retained_transcript], [retained_alignment], [], duration_ms=11_000
    )
    retained_review = local_filler_review_payload(
        "art_" + "3" * 64, retained_subtitle, duration_ms=11_000
    )
    assert retained_review["candidates"] == []
    assert retained_review["suppressions"] == []


@pytest.mark.parametrize(
    "response",
    [
        '{"suppressions":[{"cue_id":"c1","atom_id":"a1","text":"rewritten"}]}',
        '{"suppressions":[],"rewritten_text":"x"}',
    ],
)
def test_cloud_filler_parser_rejects_rewritten_text(response: str) -> None:
    with pytest.raises(ContractError):
        parse_cloud_filler_response(response)
