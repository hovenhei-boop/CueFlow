from __future__ import annotations

from cueflow.ata_provider import build_ata_submit_request
from cueflow.base_asr_provider import build_qwen_request
from cueflow.config import QWEN_ASR_MODEL
from cueflow.doubao_asr_provider import build_doubao_request
from cueflow.glm_asr_provider import build_glm_form


def test_qwen_request_has_exact_model_keywords_and_nested_filter() -> None:
    request = build_qwen_request("https://media.example/a.wav", [".NET", "GPT-5.6"])
    assert request["model"] == "qwen-audio-3.0-asr-flash-filetrans" == QWEN_ASR_MODEL
    assert request["input"] == {"file_urls": ["https://media.example/a.wav"]}
    assert request["parameters"] == {
        "channel_id": [0],
        "special_word_filter": {"system_reserved_filter": False},
        "vocabulary": [
            {"text": ".NET", "weight": 5},
            {"text": "GPT-5.6", "weight": 5},
        ],
    }
    assert "context" not in request["input"]
    assert "language_hints" not in request["parameters"]
    assert "vocabulary_id" not in request["parameters"]


def test_doubao_context_is_only_inline_hotwords() -> None:
    request = build_doubao_request("https://media.example/a.wav", ["C++"], uid="app")
    assert request == {
        "user": {"uid": "app"},
        "audio": {"url": "https://media.example/a.wav"},
        "request": {
            "model_name": "bigmodel",
            "show_utterances": True,
            "enable_ddc": False,
            "corpus": {"context": {"hotwords": [{"word": "C++"}]}},
        },
    }
    serialized = str(request)
    for forbidden in (
        "context_type",
        "context_data",
        "boosting_table",
        "correct_table",
        "regex_correct_table",
        "enable_poi_fc",
        "enable_music_fc",
        "sensitive_words_filter",
    ):
        assert forbidden not in serialized


def test_glm_has_hotwords_but_no_prompt_or_url() -> None:
    assert build_glm_form(["S&P"]) == {
        "model": "glm-asr-2512",
        "stream": "false",
        "hotwords": ["S&P"],
    }
    assert "prompt" not in build_glm_form(["S&P"])


def test_ata_uses_current_url_contract_only() -> None:
    query, payload = build_ata_submit_request("appid", "https://media.example/a.wav", "你好。")
    assert query == {"appid": "appid", "caption_type": "speech", "sta_punc_mode": "3"}
    assert payload == {"url": "https://media.example/a.wav", "audio_text": "你好。"}
    assert "caption_category" not in query
    assert "cluster" not in query
    assert "sensitive_words_filter" not in query
