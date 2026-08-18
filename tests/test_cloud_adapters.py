from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from cueflow.config import CLOUD_MODEL
from cueflow.errors import ProviderUnavailableError
from cueflow.filler import cloud_filler_review_payload, unavailable_cloud_filler_payload
from cueflow.providers import CloudOmniSemanticTranscriber


class FakeCompletions:
    def __init__(self, deltas: list[str]) -> None:
        self.deltas = deltas
        self.kwargs: dict[str, Any] | None = None

    def create(self, **kwargs: Any) -> list[Any]:
        self.kwargs = kwargs
        return [
            SimpleNamespace(
                id="response_fixture",
                choices=[SimpleNamespace(delta=SimpleNamespace(content=content))],
            )
            for content in self.deltas
        ]


class FakeClient:
    def __init__(self, completions: FakeCompletions) -> None:
        self.chat = SimpleNamespace(completions=completions)


def _factory(completions: FakeCompletions) -> Any:
    def build(**kwargs: Any) -> FakeClient:
        assert kwargs == {"api_key": "key", "base_url": "https://region.example/v1"}
        return FakeClient(completions)

    return build


def _subtitle() -> dict[str, Any]:
    return {
        "cues": [
            {
                "cue_id": "cue_00001",
                "atom_refs": [
                    {
                        "transcript_artifact_id": "art_transcript",
                        "chunk_id": "chunk_0001",
                        "atom_id": "a0001",
                        "position": 0,
                        "text": "好",
                        "atom_class": "cjk_character",
                        "decoration_after": "",
                        "global_start_ms": 0,
                        "global_end_ms": 100,
                    },
                    {
                        "transcript_artifact_id": "art_transcript",
                        "chunk_id": "chunk_0001",
                        "atom_id": "a0002",
                        "position": 1,
                        "text": "啊",
                        "atom_class": "cjk_character",
                        "decoration_after": "。",
                        "global_start_ms": 100,
                        "global_end_ms": 200,
                    },
                ],
            }
        ]
    }


def test_cloud_semantic_uses_streaming_audio_data_url_and_glossary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DASHSCOPE_API_KEY", "key")
    monkeypatch.setenv("DASHSCOPE_BASE_URL", "https://region.example/v1")
    audio = tmp_path / "chunk.wav"
    audio.write_bytes(b"RIFFfixture")
    completions = FakeCompletions(
        ['{"source_text":"完整', '逐字","language":"Chinese"}']
    )
    provider = CloudOmniSemanticTranscriber(_factory(completions))
    result = provider.transcribe(audio, ["顾华玺"], rework_context="重新核对")
    assert result.source_text == "完整逐字"
    assert result.language == "Chinese"
    assert result.response_id == "response_fixture"
    assert completions.kwargs is not None
    assert completions.kwargs["model"] == CLOUD_MODEL
    assert completions.kwargs["stream"] is True
    assert completions.kwargs["modalities"] == ["text"]
    content = completions.kwargs["messages"][0]["content"]
    assert content[0]["input_audio"]["data"].startswith("data:audio/wav;base64,")
    assert "顾华玺" in content[1]["text"]
    assert "以实际音频为准" in content[1]["text"]


def test_cloud_filler_accepts_only_candidate_ids_and_unavailable_keeps_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DASHSCOPE_API_KEY", "key")
    monkeypatch.setenv("DASHSCOPE_BASE_URL", "https://region.example/v1")
    completions = FakeCompletions(
        ['{"suppressions":[', '{"cue_id":"cue_00001","atom_id":"a0002"}]}']
    )
    payload, response_id = cloud_filler_review_payload(
        "art_subtitle",
        _subtitle(),
        duration_ms=200,
        client_factory=_factory(completions),
    )
    assert response_id == "response_fixture"
    assert payload["suppressions"][0]["text"] == "啊"
    assert completions.kwargs is not None
    assert completions.kwargs["stream"] is True
    assert completions.kwargs["modalities"] == ["text"]
    assert "response_format" not in completions.kwargs
    assert "好啊" in completions.kwargs["messages"][0]["content"]

    unavailable = unavailable_cloud_filler_payload(
        "art_subtitle", _subtitle(), duration_ms=200, reason="delivery_ambiguous"
    )
    assert unavailable["status"] == "unavailable"
    assert unavailable["suppressions"] == []
    assert unavailable["warnings"][0]["code"] == "filler_review_unavailable"


def test_cloud_semantic_requires_environment_before_send(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    monkeypatch.delenv("DASHSCOPE_BASE_URL", raising=False)
    audio = tmp_path / "chunk.wav"
    audio.write_bytes(b"fixture")
    with pytest.raises(ProviderUnavailableError):
        CloudOmniSemanticTranscriber(_factory(FakeCompletions(["unused"]))).transcribe(
            audio, []
        )
