"""One pytest run at a time on a given test database.

Two runs at once on the same PostgreSQL test database: one creates or flushes it
while the other runs, and both report failures that do not exist (deadlocks, missing
tables). The root conftest takes an exclusive, non-blocking ``flock`` before the test
database is touched; a second run on the same database stops at once.

One lock file per test database (``var/pytest-<digest>.lock``, digest of host, port
and test database name): a run on ANOTHER database, such as the child run of
tests/test_db_unreachable.py, is not blocked by the run that starts it. No setting
turns the lock off.

``flock`` belongs to the open file description: closing it, or the death of the
process (even ``kill -9``), releases the lock. A stale file never blocks a run.
"""

from __future__ import annotations

import fcntl
import hashlib
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

VAR_DIR = Path(__file__).resolve().parent.parent / "var"
MESSAGE = "Une autre exécution de pytest utilise déjà la base de test ; attendez qu'elle finisse."


class LockHeldError(Exception):
    """Another process holds the lock."""


# The lock of this pytest session, set by the root conftest. Computed once, before
# the test database is created: Django then rewrites DATABASES["default"]["NAME"]
# to the test name, and path_for() of the rewritten settings would name another file.
_session_lock: Path | None = None


def set_session_lock(path: Path | None) -> None:
    global _session_lock
    _session_lock = path


def session_lock() -> Path | None:
    return _session_lock


def path_for(database: Mapping[str, Any]) -> Path:
    """The lock file of the test database described by a DATABASES entry, as it is
    BEFORE the test database is created (see _session_lock)."""
    name = (database.get("TEST") or {}).get("NAME") or f"test_{database.get('NAME', '')}"
    where = f"{database.get('HOST') or ''}:{database.get('PORT') or ''}/{name}"
    digest = hashlib.sha256(where.encode()).hexdigest()[:16]
    return VAR_DIR / f"pytest-{digest}.lock"


def acquire(path: Path) -> int:
    """Take the lock without waiting; return the file descriptor to keep open."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        raise LockHeldError(MESSAGE) from None
    # For a human looking at the file: which process holds it.
    os.ftruncate(fd, 0)
    os.write(fd, f"{os.getpid()}\n".encode())
    return fd


def release(fd: int) -> None:
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)
