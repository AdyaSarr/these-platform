"""pytest refuses to start with settings other than config.settings.test (conftest.py).

An exported DJANGO_SETTINGS_MODULE (sourced from .env, where it names the dev
settings) wins over pyproject.toml in pytest-django: the suite then ran on the dev
settings, and dozens of tests failed for no fault of the code (console e-mail
backend, empty invitation tokens). The run must stop before collecting anything,
with a message that names what to remove.

The children run on a database server that does not exist (tests/pytest_child.py):
a child that got past the guard would stop on the database check, with another
message and another code.
"""

from __future__ import annotations

import subprocess  # nosec B404

import pytest

from tests.pytest_child import fake_database_env, run_pytest

# Child pytest runs: alone, after the parallel phase (scripts/test_all.sh).
pytestmark = pytest.mark.serial

TARGET = ("tests/test_migrations.py", "-p", "no:cacheprovider", "-q")


def _output(result: subprocess.CompletedProcess[str]) -> str:
    return result.stdout + result.stderr


@pytest.mark.parametrize("module", ["config.settings.dev", "config.settings.demo"])
def test_an_exported_settings_module_is_refused(module: str) -> None:
    env = fake_database_env("guard")
    env["DJANGO_SETTINGS_MODULE"] = module
    result = run_pytest(*TARGET, env=env)
    output = _output(result)
    assert result.returncode == 4, output  # pytest.exit(..., returncode=4) in conftest.py
    assert module in output and "config.settings.test" in output
    assert "unset DJANGO_SETTINGS_MODULE" in output
    # Before any collection, and before the database check.
    assert "collected" not in output and "docker compose up -d db" not in output


def test_the_ds_option_is_refused_too() -> None:
    env = fake_database_env("guard")
    env.pop("DJANGO_SETTINGS_MODULE", None)
    result = run_pytest(*TARGET, "--ds", "config.settings.dev", env=env)
    output = _output(result)
    assert result.returncode == 4, output
    assert "config.settings.dev" in output and "--ds" in output
    assert "collected" not in output


def test_the_test_settings_pass_the_guard_even_exported() -> None:
    env = fake_database_env("guard")
    env["DJANGO_SETTINGS_MODULE"] = "config.settings.test"
    result = run_pytest(*TARGET, env=env)
    output = _output(result)
    # Past the guard: stopped by the database check of the fake server.
    assert result.returncode == 3, output
    assert "unset DJANGO_SETTINGS_MODULE" not in output
