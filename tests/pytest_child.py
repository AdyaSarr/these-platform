"""Child pytest runs started by tests (tests/test_pytest_lock.py, tests/test_parallel_suite.py,
tests/test_db_unreachable.py).

A child inherits the environment of the test that starts it. Inside an xdist worker
that environment carries PYTEST_XDIST_WORKER and friends: they are removed, so the
child is a plain session of its own (the plugins read config.workerinput, never
these variables, but nothing should depend on that).
"""

from __future__ import annotations

import os
import subprocess  # nosec B404
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

# Port 1 is never open: a child that touched this database would fail at once.
FAKE_DATABASE = "postgres://fake:fake@127.0.0.1:1/{name}"  # pragma: allowlist secret


def child_env(**overrides: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("PYTEST_XDIST_")}
    env.update(overrides)
    return env


def fake_database_env(name: str) -> dict[str, str]:
    """A child on database ``name`` of a server that does not exist. Both variables:
    the test settings read TEST_DATABASE_URL, and would otherwise take it from .env."""
    url = FAKE_DATABASE.format(name=name)
    return child_env(DATABASE_URL=url, TEST_DATABASE_URL=url)


def run_pytest(
    *args: str, env: dict[str, str] | None = None, timeout: int = 120
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # nosec B603
        [sys.executable, "-m", "pytest", *args],
        cwd=BASE_DIR,
        env=child_env() if env is None else env,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
