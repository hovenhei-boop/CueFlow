from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest

from cueflow.errors import ContractError
from cueflow.lexicon_providers import (
    CloudLexiconExtractor,
    LexiconExtractionRequest,
    _parse_occurrences,
)


class FakeClient:
    def __init__(self) -> None:
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))
        self.calls: list[dict[str, Any]] = []
        self.closed = False

    def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        return SimpleNamespace(
            id="response-1",
            choices=[
                SimpleNamespace(message=SimpleNamespace(content='{"candidates":[]}'))
            ],
            usage=SimpleNamespace(total_tokens=2),
        )

    def close(self) -> None:
        self.closed = True


def test_cloud_extractor_reuses_preflight_client_and_closes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DASHSCOPE_API_KEY", "fake")
    monkeypatch.setenv("DASHSCOPE_BASE_URL", "https://example.test/v1")
    client = FakeClient()
    factories: list[dict[str, Any]] = []

    def factory(**kwargs: Any) -> FakeClient:
        factories.append(kwargs)
        return client

    extractor = CloudLexiconExtractor(client_factory=factory)
    extractor.preflight()
    result = extractor.extract(
        LexiconExtractionRequest(
            evidence_artifact_id="art_evidence",
            evidence_role="document_text",
            units=(
                {
                    "field_path": ["content"],
                    "base_offset": 0,
                    "text": "CueFlow",
                    "coordinates": {},
                },
            ),
        )
    )
    extractor.close()

    assert result.occurrences == ()
    assert result.response_id == "response-1"
    assert factories == [
        {"api_key": "fake", "base_url": "https://example.test/v1"}
    ]
    assert len(client.calls) == 1
    assert client.closed is True


def test_lexicon_provider_response_has_an_exact_closed_contract() -> None:
    candidate = {
        "raw_surface_form": "CueFlow",
        "field_path": ["content"],
        "start_offset": 0,
        "end_offset": 7,
        "category": "proper_noun",
        "proper_noun_subtype": "product_brand_model_software",
        "suggested_surface_form": None,
        "risk_tags": [],
    }
    assert _parse_occurrences(json.dumps({"candidates": [candidate]}))[0].field_path == (
        "content",
    )
    candidate["confidence"] = 0.9
    with pytest.raises(ContractError, match="fields"):
        _parse_occurrences(json.dumps({"candidates": [candidate]}))
