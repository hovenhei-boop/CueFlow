from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import cueflow.providers as providers
from cueflow.config import (
    LOCAL_ALIGNER_REPO,
    LOCAL_ALIGNER_REVISION,
    RuntimeConfig,
    RuntimeDeviceConfig,
)
from cueflow.errors import ProviderUnavailableError
from cueflow.orchestrator import _default_aligner_factory
from cueflow.providers import AlignmentToken, LocalQwenForcedAligner


@pytest.mark.parametrize("device,dtype", [("cpu", "float32"), ("cuda:0", "bfloat16")])
def test_aligner_uses_pinned_snapshot_and_runtime_then_releases_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, device: str, dtype: str
) -> None:
    runtime = RuntimeConfig(
        "ffmpeg", "ffprobe", str(tmp_path / "cache"), RuntimeDeviceConfig(device, dtype)
    )
    calls: list[tuple[str, Any]] = []
    dtype_value = object()

    def snapshot_download(**kwargs: Any) -> str:
        calls.append(("snapshot", kwargs))
        return str(tmp_path / "snapshot")

    def align(**kwargs: Any) -> list[list[SimpleNamespace]]:
        calls.append(("align", kwargs))
        return [[SimpleNamespace(text="测试", start_time=0.1, end_time=0.4)]]

    def from_pretrained(path: str, **kwargs: Any) -> SimpleNamespace:
        calls.append(("load", (path, kwargs)))
        return SimpleNamespace(align=align)

    modules = {
        "torch": SimpleNamespace(
            **{dtype: dtype_value},
            cuda=SimpleNamespace(
                is_available=lambda: device.startswith("cuda"),
                empty_cache=lambda: calls.append(("empty_cache", None)),
            ),
        ),
        "qwen_asr": SimpleNamespace(
            Qwen3ForcedAligner=SimpleNamespace(
                from_pretrained=from_pretrained,
            )
        ),
        "huggingface_hub": SimpleNamespace(snapshot_download=snapshot_download),
    }
    monkeypatch.setattr(providers, "import_module", modules.__getitem__)
    aligner = _default_aligner_factory(runtime)
    assert isinstance(aligner, LocalQwenForcedAligner)
    assert aligner.provider == "qwen-local"
    for _ in range(2):
        assert aligner.align(tmp_path / "audio.wav", "测试", "Chinese") == [
            AlignmentToken("测试", 100, 400)
        ]
    assert [value for name, value in calls if name == "snapshot"] == [
        {
            "repo_id": LOCAL_ALIGNER_REPO,
            "revision": LOCAL_ALIGNER_REVISION,
            "cache_dir": runtime.model_cache,
        }
    ]
    assert [value for name, value in calls if name == "load"] == [
        (str(tmp_path / "snapshot"), {"dtype": dtype_value, "device_map": device})
    ]
    assert [value for name, value in calls if name == "align"] == [
        {"audio": str(tmp_path / "audio.wav"), "text": "测试", "language": "Chinese"}
    ] * 2
    aligner.close()
    assert aligner._model is None
    assert sum(name == "empty_cache" for name, _ in calls) == int(device.startswith("cuda"))


@pytest.mark.parametrize("missing", ["torch", "qwen_asr", "huggingface_hub"])
def test_aligner_missing_dependency_fails_before_loading(
    monkeypatch: pytest.MonkeyPatch, missing: str
) -> None:
    def import_module(name: str) -> Any:
        if name == missing:
            raise ImportError(name)
        return SimpleNamespace()

    monkeypatch.setattr(providers, "import_module", import_module)
    aligner = LocalQwenForcedAligner(
        RuntimeConfig("ffmpeg", "ffprobe", None, RuntimeDeviceConfig("cpu", "float32"))
    )
    message = "pinned snapshot" if missing == "huggingface_hub" else r"cueflow\[alignment\]"
    with pytest.raises(ProviderUnavailableError, match=message):
        aligner.align(Path("audio.wav"), "测试", "Chinese")
    assert aligner._model is None
