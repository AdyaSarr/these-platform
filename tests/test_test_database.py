"""The tests run on their own PostgreSQL server (docker compose service db-test), named
by TEST_DATABASE_URL: same image as the development server, data in memory, no
fsync (docs/decisions.md). Only durability after a crash differs, never SQL.

config.settings.test is loaded in a subprocess: Django settings are configured
once per process.
"""

from __future__ import annotations

import json
import os
import re
import subprocess  # nosec B404
import sys

import pytest
from django.conf import settings

from tests.pytest_child import BASE_DIR

PROBE = """
import json, os
os.environ["DJANGO_SETTINGS_MODULE"] = "config.settings.test"
from django.conf import settings
d = settings.DATABASES["default"]
print(json.dumps({k: d.get(k) for k in ("HOST", "PORT", "NAME", "USER", "CONN_MAX_AGE")}))
"""

SECRET = "pw-not-to-echo"  # noqa: S105  # pragma: allowlist secret


def _load(test_database_url: str) -> subprocess.CompletedProcess[str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("PYTEST_XDIST_")}
    # Set, even when empty: the settings read .env without overriding the environment.
    env["TEST_DATABASE_URL"] = test_database_url
    return subprocess.run(  # nosec B603
        [sys.executable, "-c", PROBE],
        cwd=BASE_DIR,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def test_test_database_url_names_the_database_of_the_tests() -> None:
    result = _load(f"postgres://tester:{SECRET}@127.0.0.1:5433/theses")
    assert result.returncode == 0, result.stderr[-2000:]
    database = json.loads(result.stdout)
    assert (database["HOST"], database["PORT"], database["NAME"]) == ("127.0.0.1", 5433, "theses")
    assert database["USER"] == "tester"
    assert database["CONN_MAX_AGE"] == 60  # as in base.py


@pytest.mark.parametrize("host", ["localhost", "[::1]"])
def test_every_local_host_is_accepted(host: str) -> None:
    result = _load(f"postgres://tester:{SECRET}@{host}:5433/theses")
    assert result.returncode == 0, result.stderr[-2000:]


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("", "TEST_DATABASE_URL is required"),
        ("   ", "TEST_DATABASE_URL is required"),
        (f"mysql://tester:{SECRET}@127.0.0.1:5433/theses", "TEST_DATABASE_URL must be"),
        (f"postgres://tester:{SECRET}@db.example.org:5432/theses", "local"),
        (f"postgres://tester:{SECRET}@10.0.0.5:5432/theses", "local"),
        (f"postgres://tester:{SECRET}@/theses", "local"),
    ],
    ids=["empty", "blank", "not-postgres", "remote-name", "remote-address", "no-host"],
)
def test_a_missing_or_remote_test_database_is_refused(url: str, expected: str) -> None:
    """The tests create and drop databases: never on a server that is not local."""
    result = _load(url)
    assert result.returncode != 0
    assert "ImproperlyConfigured" in result.stderr
    assert expected in result.stderr
    assert SECRET not in result.stderr + result.stdout  # the URL is never echoed


def test_this_run_uses_a_local_test_server() -> None:
    database = settings.DATABASES["default"]
    assert database["HOST"] in {"127.0.0.1", "localhost", "::1"}


def _service(name: str) -> str:
    """The block of a top-level service of docker-compose.yml, as text."""
    text = (BASE_DIR / "docker-compose.yml").read_text()
    match = re.search(rf"^  {re.escape(name)}:\n((?:(?:    .*)?\n)+)", text, flags=re.MULTILINE)
    assert match is not None, name
    return match.group(1)


def test_the_test_server_is_the_development_image_in_memory_without_fsync() -> None:
    test, dev = _service("db-test"), _service("db")
    image = re.compile(r"^    image: (\S+)$", re.MULTILINE)
    assert image.findall(test) == image.findall(dev) == ["postgres:16-alpine"]
    assert "tmpfs:" in test and "/var/lib/postgresql/data" in test
    assert "volumes:" not in test  # nothing written to disk, nothing kept
    for option in ("fsync=off", "synchronous_commit=off", "full_page_writes=off"):
        assert option in test
    assert '"127.0.0.1:5433:5432"' in test  # local only, beside the development server
    assert "${TEST_POSTGRES_PASSWORD:?" in test  # compose refuses to start without it


def test_env_example_documents_the_test_database() -> None:
    text = (BASE_DIR / ".env.example").read_text()
    assert re.search(r"^TEST_POSTGRES_PASSWORD=", text, flags=re.MULTILINE)
    url = re.search(r"^TEST_DATABASE_URL=(\S+)$", text, flags=re.MULTILINE)
    assert url is not None and "@127.0.0.1:5433/" in url.group(1)
