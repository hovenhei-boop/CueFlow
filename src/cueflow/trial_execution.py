from __future__ import annotations

import threading
from collections.abc import Callable

from cueflow.errors import TrialExecutionStopped
from cueflow.trial_store import TrialStore, now_utc

StoreFactory = Callable[[], TrialStore]


class TrialExecutionGate:
    def __init__(self, store_factory: StoreFactory, request_id: str) -> None:
        self._store_factory = store_factory
        self._request_id = request_id

    def before_invocation(self, run_id: str, operation: str) -> None:
        store = self._store_factory()
        try:
            allowed = store.authorize_invocation(
                self._request_id, run_id, now=now_utc()
            )
        finally:
            store.close()
        if not allowed:
            raise TrialExecutionStopped(
                f"Trial execution stopped before {operation}; no invocation was created"
            )


class TrialAlivePulse:
    """Independent request heartbeat; a stopped request never resurrects itself."""

    def __init__(
        self, store_factory: StoreFactory, request_id: str, *, interval_seconds: float
    ) -> None:
        self._store_factory = store_factory
        self._request_id = request_id
        self._interval_seconds = interval_seconds
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._run, name=f"trial-alive-{request_id}", daemon=True
        )

    def __enter__(self) -> TrialAlivePulse:
        self._touch()
        self._thread.start()
        return self

    def __exit__(self, *_: object) -> None:
        self._stop.set()
        self._thread.join(timeout=max(1.0, self._interval_seconds * 2))

    def _run(self) -> None:
        while not self._stop.wait(self._interval_seconds):
            if not self._touch():
                return

    def _touch(self) -> bool:
        store = self._store_factory()
        try:
            return store.touch_alive(self._request_id, now=now_utc())
        finally:
            store.close()
