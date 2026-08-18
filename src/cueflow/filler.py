from __future__ import annotations

import json
import os
from collections.abc import Callable, Mapping
from importlib import import_module
from typing import Any, cast

from cueflow.canonical import hash_json
from cueflow.config import CLOUD_MODEL, FillerReviewConfig
from cueflow.errors import (
    ContractError,
    DeliveryAmbiguousError,
    ProviderError,
    ProviderUnavailableError,
)
from cueflow.providers import collect_cloud_text_stream, parse_cloud_filler_response
from cueflow.schema import validate_filler_review_payload


def filler_candidates(
    subtitle: Mapping[str, Any], *, duration_ms: int
) -> list[dict[str, Any]]:
    config = FillerReviewConfig()
    cues = subtitle.get("cues")
    if not isinstance(cues, list):
        raise ContractError("Subtitle cues are missing")
    all_refs = [ref for cue in cues for ref in cue.get("atom_refs", [])]
    next_by_key: dict[tuple[str, str], Mapping[str, Any] | None] = {}
    for index, ref in enumerate(all_refs):
        key = (str(ref["transcript_artifact_id"]), str(ref["atom_id"]))
        next_by_key[key] = all_refs[index + 1] if index + 1 < len(all_refs) else None
    candidates: list[dict[str, Any]] = []
    for cue in cues:
        refs = cue.get("atom_refs")
        if not isinstance(refs, list) or len(refs) < 2:
            continue
        final = refs[-1]
        text = str(final["text"])
        if text not in config.whitelist:
            continue
        key = (str(final["transcript_artifact_id"]), str(final["atom_id"]))
        following = next_by_key[key]
        following_pause = (
            int(following["global_start_ms"]) - int(final["global_end_ms"])
            if following is not None
            else None
        )
        stream_terminal = (
            following is None
            or str(following["chunk_id"]) != str(final["chunk_id"])
            or duration_ms - int(final["global_end_ms"]) <= 20
        )
        candidates.append(
            {
                "cue_id": str(cue["cue_id"]),
                "transcript_artifact_id": str(final["transcript_artifact_id"]),
                "atom_id": str(final["atom_id"]),
                "text": text,
                "evidence": {
                    "cue_terminal": True,
                    "sentence_terminal_decoration": _terminal_decoration(
                        str(final.get("decoration_after", ""))
                    ),
                    "following_pause_ms": following_pause,
                    "stream_terminal": stream_terminal,
                },
            }
        )
    return candidates


def local_filler_review_payload(
    subtitle_artifact_id: str,
    subtitle: Mapping[str, Any],
    *,
    duration_ms: int,
) -> dict[str, Any]:
    config = FillerReviewConfig()
    candidates = filler_candidates(subtitle, duration_ms=duration_ms)
    suppressions = []
    for candidate in candidates:
        evidence = candidate["evidence"]
        pause = evidence["following_pause_ms"]
        clear_terminal = bool(evidence["sentence_terminal_decoration"]) and (
            bool(evidence["stream_terminal"])
            or (isinstance(pause, int) and pause >= config.local_pause_ms)
        )
        if clear_terminal:
            suppressions.append(_suppression(candidate))
    payload = {
        "subtitle_artifact_id": subtitle_artifact_id,
        "review_config_hash": hash_json(
            {
                "version": config.version,
                "whitelist": list(config.whitelist),
                "local_pause_ms": config.local_pause_ms,
            }
        ),
        "mode": "deterministic_local",
        "status": "completed",
        "candidates": candidates,
        "suppressions": suppressions,
        "warnings": [],
    }
    validate_filler_review_payload(payload)
    return payload


def cloud_filler_review_payload(
    subtitle_artifact_id: str,
    subtitle: Mapping[str, Any],
    *,
    duration_ms: int,
    client_factory: Callable[..., Any] | None = None,
) -> tuple[dict[str, Any], str | None]:
    candidates = filler_candidates(subtitle, duration_ms=duration_ms)
    config = FillerReviewConfig()
    api_key = os.getenv("DASHSCOPE_API_KEY")
    base_url = os.getenv("DASHSCOPE_BASE_URL")
    if not api_key or not base_url:
        raise ProviderUnavailableError(
            "Cloud Filler Review requires DASHSCOPE_API_KEY and DASHSCOPE_BASE_URL"
        )
    factory = client_factory or _openai_factory()
    cue_contexts = [
        {
            "cue_id": str(cue["cue_id"]),
            "verbatim_text": "".join(
                str(ref["text"]) + str(ref.get("decoration_after", ""))
                for ref in cue.get("atom_refs", [])
            ),
        }
        for cue in subtitle.get("cues", [])
    ]
    prompt = {
        "instruction": (
            "只判断候选是否是可隐藏且不改变语义的句尾水词。不得改写文字。"
            "严格返回 JSON 对象 {\"suppressions\":[{\"cue_id\":...,\"atom_id\":...}]}。"
            "有歧义必须保留，不要返回该候选。"
        ),
        "cues": cue_contexts,
        "candidates": candidates,
    }
    try:
        client = factory(api_key=api_key, base_url=base_url)
    except Exception as exc:
        raise ProviderUnavailableError("cloud client could not be created") from exc
    try:
        response = client.chat.completions.create(
            model=CLOUD_MODEL,
            messages=[
                {
                    "role": "user",
                    "content": json.dumps(prompt, ensure_ascii=False, separators=(",", ":")),
                }
            ],
            modalities=["text"],
            stream=True,
            stream_options={"include_usage": True},
            temperature=0,
        )
        content, response_id = collect_cloud_text_stream(response)
    except Exception as exc:
        if isinstance(exc, ContractError):
            raise
        if getattr(exc, "status_code", None) is not None:
            raise ProviderError(f"cloud filler provider explicit failure: {exc}") from exc
        raise DeliveryAmbiguousError(
            "cloud filler request may have been delivered; automatic retry is forbidden"
        ) from exc
    reviewed = parse_cloud_filler_response(content)
    candidate_map = {
        (str(item["cue_id"]), str(item["atom_id"])): item for item in candidates
    }
    seen_cues: set[str] = set()
    suppressions: list[dict[str, Any]] = []
    for item in reviewed:
        key = (item["cue_id"], item["atom_id"])
        candidate = candidate_map.get(key)
        if candidate is None or item["cue_id"] in seen_cues:
            raise ContractError(
                "cloud filler response references an unknown or duplicate candidate"
            )
        seen_cues.add(item["cue_id"])
        suppressions.append(_suppression(candidate))
    payload = {
        "subtitle_artifact_id": subtitle_artifact_id,
        "review_config_hash": hash_json(
            {"version": config.version, "whitelist": list(config.whitelist), "model": CLOUD_MODEL}
        ),
        "mode": "cloud_atom_review",
        "status": "completed",
        "candidates": candidates,
        "suppressions": suppressions,
        "warnings": [],
    }
    validate_filler_review_payload(payload)
    return payload, response_id


def unavailable_cloud_filler_payload(
    subtitle_artifact_id: str,
    subtitle: Mapping[str, Any],
    *,
    duration_ms: int,
    reason: str,
) -> dict[str, Any]:
    config = FillerReviewConfig()
    payload = {
        "subtitle_artifact_id": subtitle_artifact_id,
        "review_config_hash": hash_json(
            {"version": config.version, "whitelist": list(config.whitelist), "model": CLOUD_MODEL}
        ),
        "mode": "cloud_atom_review",
        "status": "unavailable",
        "candidates": filler_candidates(subtitle, duration_ms=duration_ms),
        "suppressions": [],
        "warnings": [{"code": "filler_review_unavailable", "reason": reason}],
    }
    validate_filler_review_payload(payload)
    return payload


def _suppression(candidate: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "cue_id": str(candidate["cue_id"]),
        "transcript_artifact_id": str(candidate["transcript_artifact_id"]),
        "atom_id": str(candidate["atom_id"]),
        "text": str(candidate["text"]),
        "reason": "terminal_filler",
    }


def _terminal_decoration(value: str) -> bool:
    return any(character in "。；;？?！!." for character in value)


def _openai_factory() -> Callable[..., Any]:
    try:
        module = import_module("openai")
    except ImportError as exc:
        raise ProviderUnavailableError(
            "CLOUD_PROFILE requires the cueflow[cloud] dependencies"
        ) from exc
    return cast(Callable[..., Any], module.OpenAI)
