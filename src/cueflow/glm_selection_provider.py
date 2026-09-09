from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from importlib.resources import files
from typing import Any, Protocol

from cueflow.asr_contracts import ProviderMetadata
from cueflow.cloud_stream import CompletedResponseError, complete_json, openai_factory
from cueflow.config import GLM_SELECTION_MODEL, SelectionConfig
from cueflow.conflict_selection import validate_decisions
from cueflow.errors import ContractError, ProviderUnavailableError

PROMPT_VERSION = "transcript-candidate-selection-zh-v1"


def load_selection_prompt() -> tuple[str, str]:
    prompt = (
        files("cueflow")
        .joinpath("prompts/transcript_candidate_selection_zh_v1.txt")
        .read_text(encoding="utf-8")
    )
    return prompt, "sha256:" + hashlib.sha256(prompt.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class SelectionResult:
    decisions: tuple[Mapping[str, str], ...]
    metadata: ProviderMetadata


class SelectionProvider(Protocol):
    provider: str
    model: str

    def select(self, request: Mapping[str, Any]) -> SelectionResult: ...

    def close(self) -> None: ...


class GlmSelectionProvider:
    provider = "zhipu-openai-compatible"
    model = GLM_SELECTION_MODEL

    def __init__(self, client_factory: Callable[..., Any] | None = None) -> None:
        self._client_factory = client_factory

    def select(self, request: Mapping[str, Any]) -> SelectionResult:
        api_key = os.getenv("ZHIPU_API_KEY")
        base_url = os.getenv("ZHIPU_BASE_URL") or "https://open.bigmodel.cn/api/paas/v4"
        if not api_key:
            raise ProviderUnavailableError("GLM selection requires ZHIPU_API_KEY")
        config = SelectionConfig()
        encoded = json.dumps(request, ensure_ascii=False, separators=(",", ":"))
        if len(encoded.encode("utf-8")) > config.max_input_bytes:
            raise ContractError("Selection input exceeds frozen request budget")
        prompt, _ = load_selection_prompt()
        value, metadata, raw_text = complete_json(
            self._client_factory or openai_factory(),
            api_key=api_key,
            base_url=base_url,
            provider=self.provider,
            model=self.model,
            body={
                "messages": [
                    {"role": "system", "content": prompt},
                    {"role": "user", "content": encoded},
                ],
                "temperature": 0,
                "max_tokens": config.max_output_tokens,
                "response_format": {"type": "json_object"},
                "extra_body": {"thinking": {"type": "disabled"}},
                "tool_choice": "auto",
                "tools": [
                    {
                        "type": "web_search",
                        "web_search": {
                            "enable": config.web_search,
                            "search_engine": "search_pro",
                            "search_result": True,
                        },
                    }
                ],
            },
        )
        try:
            decisions = validate_decisions(value, request)
        except ContractError as exc:
            raise CompletedResponseError(
                str(exc), metadata, raw_response=raw_text, finish_reason="stop"
            ) from exc
        return SelectionResult(tuple(decisions), metadata)

    def close(self) -> None:
        return None
