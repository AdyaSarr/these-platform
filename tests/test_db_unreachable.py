"""pytest must stop with an actionable message when PostgreSQL is unreachable."""

import pytest

from tests.pytest_child import FAKE_DATABASE, child_env, run_pytest

# Child pytest runs and timings: alone, after the parallel phase (scripts/test_all.sh).
pytestmark = pytest.mark.serial


def test_pytest_exits_with_clear_message_without_database() -> None:
    env = child_env(DATABASE_URL=FAKE_DATABASE.format(name="fake"))
    result = run_pytest("tests/test_migrations.py", "-p", "no:cacheprovider", "-q", env=env)
    output = result.stdout + result.stderr
    assert result.returncode == 3, output  # pytest.exit(..., returncode=3) in conftest.py
    assert "docker compose up -d db" in output
