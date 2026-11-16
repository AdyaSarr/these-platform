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

Under pytest-xdist (scripts/test_all.sh) the lock is held by the CONTROLLER only, in
pytest_configure, before any worker starts: every ``test_<name>_gwN`` database
derives from the same configured database, so its one lock covers them all, and a
second session, whatever its ``-n``, stops at once. The workers never take it (they
would block each other): each checks, before its database is created, that its
parent holds it (check_held). Each worker writes the tests it runs, in order, to
``var/pytest-order/<digest>/gwN.txt`` (order_dir), protected by the same lock.
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


class LockNotHeldError(Exception):
    """The lock a worker relies on is not held by its controller."""


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


def check_held(path: Path, *, holder_pid: int) -> None:
    """Check, without taking it, that the lock ``path`` is held by ``holder_pid``.

    The probe is a separate open file description: if the lock turns out to be
    free, the probe takes and drops it at once, and closing it never releases the
    lock of another description.
    """
    try:
        fd = os.open(path, os.O_RDONLY)
    except FileNotFoundError:
        raise LockNotHeldError(f"{path.name}: no lock file") from None
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            pass  # held by someone: check who below
        else:
            fcntl.flock(fd, fcntl.LOCK_UN)
            raise LockNotHeldError(f"{path.name}: nobody holds it")
        written = os.read(fd, 32).strip()
    finally:
        os.close(fd)
    if written != str(holder_pid).encode():
        raise LockNotHeldError(f"{path.name}: held by {written.decode(errors='replace')!r}")


def order_dir(lock: Path) -> Path:
    """Where the workers of the session holding ``lock`` write the order of their tests."""
    return VAR_DIR / "pytest-order" / lock.stem.removeprefix("pytest-")
