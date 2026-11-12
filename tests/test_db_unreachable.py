"""pytest must stop with an actionable message when PostgreSQL is unreachable."""

import os
import subprocess
import sys

from tests.prod_helpers import BASE_DIR


def test_pytest_exits_with_clear_message_without_database() -> None:
    env = dict(os.environ)
    # Port 1 is never open.
    env["DATABASE_URL"] = "postgres://fake:fake@127.0.0.1:1/fake"  # pragma: allowlist secret
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "tests/test_migrations.py",
            "-p",
            "no:cacheprovider",
            "-q",
        ],
        cwd=BASE_DIR,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    output = result.stdout + result.stderr
    assert result.returncode == 3, output  # pytest.exit(..., returncode=3) in conftest.py
    assert "docker compose up -d db" in output
