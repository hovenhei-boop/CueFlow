from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from cueflow.config import CLOUD_MODEL
from cueflow.errors import ProviderUnavailableError
from cueflow.orchestrator import _default_semantic_factory
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


def test_default_semantic_factory_builds_remote_provider() -> None:
    provider = _default_semantic_factory()
    assert isinstance(provider, CloudOmniSemanticTranscriber)
    provider.close()


class FakeClient:
    def __init__(self, completions: FakeCompletions) -> None:
        self.chat = SimpleNamespace(completions=completions)


def _factory(completions: FakeCompletions) -> Any:
    def build(**kwargs: Any) -> FakeClient:
        assert kwargs == {"api_key": "key", "base_url": "https://region.example/v1"}
        return FakeClient(completions)

    return build


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
