from __future__ import annotations

from collections import namedtuple
from pathlib import Path

import pytest
from trial_helpers import trial_config

from cueflow.trial_storage import (
    LifecycleRule,
    check_disk_capacity,
    evaluate_storage_readiness,
)


class Inspector:
    def __init__(self, rules: list[LifecycleRule]) -> None:
        self._rules = rules

    def check_bucket_access(self) -> None:
        return None

    def check_object_access(self, prefix: str, url_ttl_seconds: int) -> None:
        del prefix, url_ttl_seconds

    def rules(self) -> list[LifecycleRule]:
        return self._rules


def test_disk_admission_requires_ratio_and_peak_temporary_space(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    usage = namedtuple("usage", "total used free")
    monkeypatch.setattr("cueflow.trial_storage.shutil.disk_usage", lambda _: usage(1000, 900, 100))
    ratio_failure = check_disk_capacity(
        tmp_path, global_concurrency=2, active_concurrency=1, max_source_bytes=100,
        expansion_factor=2, safety_margin_bytes=100, max_used_ratio=0.85,
    )
    assert not ratio_failure.ready

    monkeypatch.setattr("cueflow.trial_storage.shutil.disk_usage", lambda _: usage(1000, 100, 250))
    free_failure = check_disk_capacity(
        tmp_path, global_concurrency=2, active_concurrency=1, max_source_bytes=100,
        expansion_factor=2, safety_margin_bytes=100, max_used_ratio=0.85,
    )
    assert free_failure.required_free_bytes == 300
    assert not free_failure.ready

    monkeypatch.setattr("cueflow.trial_storage.shutil.disk_usage", lambda _: usage(1000, 100, 300))
    assert check_disk_capacity(
        tmp_path, global_concurrency=2, active_concurrency=1, max_source_bytes=100,
        expansion_factor=2, safety_margin_bytes=100, max_used_ratio=0.85,
    ).ready


def test_lifecycle_requires_short_source_work_and_unexpired_result(tmp_path: Path) -> None:
    config = trial_config(tmp_path)
    ready = evaluate_storage_readiness(Inspector([
        LifecycleRule("trial/source", True, 7),
        LifecycleRule("trial/work", True, 7),
    ]), config)
    assert ready.ready

    expired_result = evaluate_storage_readiness(Inspector([
        LifecycleRule("trial", True, 7),
    ]), config)
    assert not expired_result.ready
    assert any("result" in reason for reason in expired_result.reasons)
