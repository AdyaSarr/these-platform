"""One pytest run at a time (tests/pytest_lock.py): a held lock stops a second run at
once, with a clear message, before the test database is touched; the death of the
holder releases it."""

from __future__ import annotations

import os
import signal
import subprocess  # nosec B404
import sys
import time
from pathlib import Path

import pytest

from tests import pytest_lock

BASE_DIR = Path(__file__).resolve().parent.parent

HOLDER = """
import fcntl, os, sys, time
fd = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT, 0o600)
fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
print("held", flush=True)
time.sleep(120)
"""


def test_a_held_lock_refuses_a_second_taker_at_once(tmp_path: Path) -> None:
    lock = tmp_path / "pytest.lock"
    holder = subprocess.Popen(  # nosec B603
        [sys.executable, "-c", HOLDER, str(lock)], stdout=subprocess.PIPE, text=True
    )
    try:
        assert holder.stdout is not None and holder.stdout.readline().strip() == "held"
        start = time.monotonic()
        with pytest.raises(pytest_lock.LockHeldError) as info:
            pytest_lock.acquire(lock)
        assert time.monotonic() - start < 1.0  # never waits
        assert str(info.value) == pytest_lock.MESSAGE
    finally:
        os.kill(holder.pid, signal.SIGKILL)  # no cleanup code runs in the holder
        holder.wait(timeout=10)
    # The kernel released the lock with the process: the stale file blocks nothing.
    fd = pytest_lock.acquire(lock)
    pytest_lock.release(fd)


def test_a_second_session_stops_at_once() -> None:
    """This session holds the lock of its test database: a second pytest run on the
    same database must stop before running any test (hence before creating or
    flushing the test database)."""
    start = time.monotonic()
    result = subprocess.run(  # nosec B603
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-p",
            "no:randomly",
            "apps/travel/tests/test_holidays.py::test_public_holidays_of_2027",
        ],
        cwd=BASE_DIR,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    output = result.stdout + result.stderr
    assert result.returncode == 3, output[-2000:]
    assert pytest_lock.MESSAGE in output
    assert "passed" not in output
    assert time.monotonic() - start < 60


def test_one_lock_per_test_database() -> None:
    base = {"NAME": "these", "HOST": "127.0.0.1", "PORT": "5432", "TEST": {}}
    same = pytest_lock.path_for(dict(base))
    assert same == pytest_lock.path_for(dict(base))
    assert same.parent == BASE_DIR / "var"
    others = [
        {**base, "NAME": "other"},
        {**base, "HOST": "db.example.org"},
        {**base, "PORT": "1"},
        {**base, "TEST": {"NAME": "test_custom"}},
    ]
    assert len({same, *(pytest_lock.path_for(o) for o in others)}) == 5
    # Explicit TEST NAME equal to Django's default: the same database, the same lock.
    assert pytest_lock.path_for({**base, "TEST": {"NAME": "test_these"}}) == same


def test_this_session_holds_the_lock_of_its_database(db: None) -> None:
    """Asked for after the test database exists (the db fixture): the lock held is
    still the one of the configured database, and another taker is refused."""
    held = pytest_lock.session_lock()
    assert held is not None and held.parent == BASE_DIR / "var"
    with pytest.raises(pytest_lock.LockHeldError):
        pytest_lock.acquire(held)


def test_the_lock_files_are_ignored_by_git() -> None:
    held = pytest_lock.session_lock()
    assert held is not None
    path = held.relative_to(BASE_DIR)
    result = subprocess.run(  # nosec B603 B607
        ["git", "check-ignore", "-q", str(path)], cwd=BASE_DIR, check=False
    )
    assert result.returncode == 0
