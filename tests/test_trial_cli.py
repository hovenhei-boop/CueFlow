from __future__ import annotations

import importlib.util

import pytest

from cueflow.trial_cli import _require_trial_extra


def test_trial_serve_missing_extra_has_install_instruction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(importlib.util, "find_spec", lambda _: None)
    with pytest.raises(RuntimeError, match=r'pip install "cueflow\[trial\]"'):
        _require_trial_extra()
