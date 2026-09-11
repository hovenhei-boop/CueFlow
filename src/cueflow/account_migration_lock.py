from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from importlib import import_module
from pathlib import Path
from typing import BinaryIO

from cueflow.errors import AccountMigrationLockedError


@contextmanager
def account_migration_lock(lock_path: Path) -> Iterator[None]:
    """Fail-fast process lock dedicated to one Account database migration."""

    if not lock_path.is_absolute():
        raise AccountMigrationLockedError("Account migration lock path must be absolute")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+b") as lock:
        if lock.tell() == 0:
            lock.write(b"0")
            lock.flush()
        lock.seek(0)
        _lock(lock)
        try:
            yield
        finally:
            lock.seek(0)
            _unlock(lock)


def _lock(lock: BinaryIO) -> None:
    try:
        if os.name == "nt":
            msvcrt = import_module("msvcrt")
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            fcntl = import_module("fcntl")
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        raise AccountMigrationLockedError(
            "another CueFlow process is migrating the Account database"
        ) from exc


def _unlock(lock: BinaryIO) -> None:
    if os.name == "nt":
        msvcrt = import_module("msvcrt")
        msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        fcntl = import_module("fcntl")
        fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
