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
from tests.pytest_child import run_pytest

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


# ------------------------------------------------------------------ parallel sessions
# Under pytest-xdist the lock is held by the controller only (root conftest,
# pytest_configure); each worker checks, before its test database is created, that
# its parent holds it (pytest_lock.check_held).

HOLDER_WITH_PID = """
import fcntl, os, sys, time
fd = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT, 0o600)
fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
os.ftruncate(fd, 0)
os.write(fd, f"{os.getpid()}\\n".encode())
print("held", flush=True)
time.sleep(120)
"""


def test_check_held_refuses_a_missing_lock_file(tmp_path: Path) -> None:
    with pytest.raises(pytest_lock.LockNotHeldError):
        pytest_lock.check_held(tmp_path / "missing.lock", holder_pid=os.getpid())


def test_check_held_refuses_a_free_lock_and_leaves_it_free(tmp_path: Path) -> None:
    lock = tmp_path / "pytest.lock"
    pytest_lock.release(pytest_lock.acquire(lock))  # the file exists, nobody holds it
    with pytest.raises(pytest_lock.LockNotHeldError):
        pytest_lock.check_held(lock, holder_pid=os.getpid())
    pytest_lock.release(pytest_lock.acquire(lock))  # the check did not keep it


def test_check_held_accepts_only_the_expected_holder(tmp_path: Path) -> None:
    lock = tmp_path / "pytest.lock"
    holder = subprocess.Popen(  # nosec B603
        [sys.executable, "-c", HOLDER_WITH_PID, str(lock)], stdout=subprocess.PIPE, text=True
    )
    try:
        assert holder.stdout is not None and holder.stdout.readline().strip() == "held"
        pytest_lock.check_held(lock, holder_pid=holder.pid)
        with pytest.raises(pytest_lock.LockNotHeldError):
            pytest_lock.check_held(lock, holder_pid=os.getpid())  # held, but by another
    finally:
        os.kill(holder.pid, signal.SIGKILL)
        holder.wait(timeout=10)
    with pytest.raises(pytest_lock.LockNotHeldError):
        pytest_lock.check_held(lock, holder_pid=holder.pid)  # its death released it


def test_a_second_parallel_session_stops_before_starting_workers() -> None:
    """Same database as this session: the controller of the child is refused the lock
    in pytest_configure, before xdist starts any worker or creates any database."""
    start = time.monotonic()
    result = run_pytest(
        "-n", "2", "-p", "no:randomly",
        "apps/travel/tests/test_holidays.py::test_public_holidays_of_2027",
    )  # fmt: skip
    output = result.stdout + result.stderr
    assert result.returncode == 3, output[-2000:]
    assert pytest_lock.MESSAGE in output
    assert "passed" not in output
    assert "gw0" not in output  # no worker was started
    assert time.monotonic() - start < 60


def test_order_files_are_kept_next_to_the_lock_of_their_database() -> None:
    held = pytest_lock.session_lock()
    assert held is not None
    folder = pytest_lock.order_dir(held)
    assert folder.parent == BASE_DIR / "var" / "pytest-order"
    assert held.name == f"pytest-{folder.name}.lock"
    other = pytest_lock.path_for({"NAME": "other", "HOST": "", "PORT": "", "TEST": {}})
    assert pytest_lock.order_dir(other) != folder
