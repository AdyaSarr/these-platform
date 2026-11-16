"""The database server of the tests (docs/decisions.md).

- TEST_DATABASE_URL, optional, names a dedicated local server (docker compose
  service db-test: the development image, data in memory, no fsync). Absent or
  empty: the tests use DATABASE_URL, as before, and pytest says so at the end of
  its output (never when the settings are imported).
- Only db-test runs without fsync and in memory; db keeps its volume (static guard).
- mypy (django-stubs) loads config.settings.typecheck, which needs no variable.

Settings are loaded in a subprocess: Django settings are configured once per process.
"""

from __future__ import annotations

import json
import re
import subprocess  # nosec B404
import sys
from pathlib import Path

import pytest
from django.conf import settings

from tests.pytest_child import BASE_DIR, FAKE_DATABASE, child_env, run_pytest

PROBE = """
import json, os
import environ
os.environ["DJANGO_SETTINGS_MODULE"] = "config.settings.test"
from django.conf import settings
keys = ("HOST", "PORT", "NAME", "USER")
d = settings.DATABASES["default"]
dev = environ.Env.db_url_config(os.environ["DATABASE_URL"])
print(json.dumps({
    "test": {k: d.get(k) for k in (*keys, "CONN_MAX_AGE", "CONN_HEALTH_CHECKS")},
    "dev": {k: dev.get(k) for k in keys},
    "source": settings.TEST_DATABASE_SOURCE,
}))
"""

SECRET = "pw-not-to-echo"  # noqa: S105  # pragma: allowlist secret
FALLBACK_MESSAGE = "TEST_DATABASE_URL absente"


def _load(test_database_url: str) -> subprocess.CompletedProcess[str]:
    # Set, even when empty: the settings read .env without overriding the environment.
    env = child_env(TEST_DATABASE_URL=test_database_url)
    return subprocess.run(  # nosec B603
        [sys.executable, "-c", PROBE],
        cwd=BASE_DIR,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


# ------------------------------------------------------------------ TEST_DATABASE_URL


def test_test_database_url_names_the_database_of_the_tests() -> None:
    result = _load(f"postgres://tester:{SECRET}@127.0.0.1:5433/theses")
    assert result.returncode == 0, result.stderr[-2000:]
    loaded = json.loads(result.stdout)
    database = loaded["test"]
    assert (database["HOST"], database["PORT"], database["NAME"]) == ("127.0.0.1", 5433, "theses")
    assert database["USER"] == "tester"
    assert (database["CONN_MAX_AGE"], database["CONN_HEALTH_CHECKS"]) == (60, True)  # as base.py
    assert loaded["source"] == "TEST_DATABASE_URL"


@pytest.mark.parametrize("value", ["", "   "], ids=["empty", "blank"])
def test_without_test_database_url_the_tests_use_database_url_as_before(value: str) -> None:
    result = _load(value)
    assert result.returncode == 0, result.stderr[-2000:]
    loaded = json.loads(result.stdout)
    assert {k: loaded["test"][k] for k in loaded["dev"]} == loaded["dev"]
    assert loaded["source"] == "DATABASE_URL"
    assert FALLBACK_MESSAGE not in result.stdout + result.stderr  # nothing said at import


@pytest.mark.parametrize("host", ["localhost", "[::1]"])
def test_every_local_host_is_accepted(host: str) -> None:
    result = _load(f"postgres://tester:{SECRET}@{host}:5433/theses")
    assert result.returncode == 0, result.stderr[-2000:]


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (f"mysql://tester:{SECRET}@127.0.0.1:5433/theses", "TEST_DATABASE_URL must be"),
        (f"postgres://tester:{SECRET}@db.example.org:5432/theses", "local"),
        (f"postgres://tester:{SECRET}@10.0.0.5:5432/theses", "local"),
        (f"postgres://tester:{SECRET}@/theses", "local"),
    ],
    ids=["not-postgres", "remote-name", "remote-address", "no-host"],
)
def test_a_test_database_url_that_is_not_local_postgres_is_refused(url: str, expected: str) -> None:
    """The tests create and drop databases: never on a server that is not local."""
    result = _load(url)
    assert result.returncode != 0
    assert "ImproperlyConfigured" in result.stderr
    assert expected in result.stderr
    assert SECRET not in result.stderr + result.stdout  # the URL is never echoed


def test_this_run_names_its_database_source() -> None:
    assert settings.TEST_DATABASE_SOURCE in {"TEST_DATABASE_URL", "DATABASE_URL"}


PURE_TEST = "apps/demo/tests/test_demo_db.py::test_demo_names_are_accepted"


@pytest.mark.serial
def test_pytest_says_at_the_end_when_it_falls_back_to_database_url() -> None:
    env = child_env(DATABASE_URL=FAKE_DATABASE.format(name="fallback"), TEST_DATABASE_URL="")
    result = run_pytest("-q", "-p", "no:xdist", "-p", "no:randomly", PURE_TEST, env=env)
    output = result.stdout + result.stderr
    assert result.returncode == 0, output[-2000:]
    tail = "\n".join(output.splitlines()[-4:])
    assert FALLBACK_MESSAGE in tail and "docker compose up -d db-test" in tail


@pytest.mark.serial
def test_pytest_says_nothing_with_a_test_database_url() -> None:
    url = FAKE_DATABASE.format(name="dedicated")  # 127.0.0.1: local
    env = child_env(DATABASE_URL=url, TEST_DATABASE_URL=url)
    result = run_pytest("-q", "-p", "no:xdist", "-p", "no:randomly", PURE_TEST, env=env)
    output = result.stdout + result.stderr
    assert result.returncode == 0, output[-2000:]
    assert FALLBACK_MESSAGE not in output


# ------------------------------------------------------------------ mypy settings

TYPECHECK_PROBE = """
import json, os
os.environ["DJANGO_SETTINGS_MODULE"] = "config.settings.typecheck"
from django.conf import settings
d = settings.DATABASES["default"]
print(json.dumps({"apps": settings.INSTALLED_APPS, "user": settings.AUTH_USER_MODEL,
                  "db": [d["HOST"], d["PORT"]], "broker": settings.CELERY_BROKER_URL}))
"""


def test_typecheck_settings_load_in_an_empty_environment_like_the_test_ones() -> None:
    result = subprocess.run(  # nosec B603
        [sys.executable, "-c", TYPECHECK_PROBE],
        cwd=BASE_DIR,
        env={},
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr[-2000:]
    loaded = json.loads(result.stdout)
    assert loaded["apps"] == list(settings.INSTALLED_APPS)  # same models for mypy
    assert loaded["user"] == settings.AUTH_USER_MODEL
    assert loaded["db"] == ["127.0.0.1", 1] and ":1/" in loaded["broker"]  # never reachable


def test_typecheck_settings_read_no_file_and_are_used_by_django_stubs_only() -> None:
    import tomllib

    source = (BASE_DIR / "config" / "settings" / "typecheck.py").read_text()
    assert "read_env" not in source and ".env" not in source.replace("os.environ", "")
    config = tomllib.loads((BASE_DIR / "pyproject.toml").read_text())
    assert config["tool"]["django-stubs"]["django_settings_module"] == "config.settings.typecheck"
    users = [
        p.relative_to(BASE_DIR).as_posix()
        for root in ("apps", "config", "scripts", "tests")
        for p in (BASE_DIR / root).rglob("*")
        if p.is_file()
        and p.suffix in {".py", ".sh", ".toml", ".cfg"}
        and "settings.typecheck" in p.read_text(errors="replace")
    ]
    assert set(users) <= {"config/settings/typecheck.py", "tests/test_test_database.py"}


@pytest.mark.serial  # a full mypy run: long, and sensitive to load
def test_mypy_needs_no_variable(tmp_path: Path) -> None:
    """No database, Redis or secret variable: mypy with an empty environment, on a
    module that uses models (the django-stubs plugin loads every app)."""
    result = subprocess.run(  # nosec B603
        [
            sys.executable, "-m", "mypy", "--cache-dir", str(tmp_path / "cache"),
            "config/settings/typecheck.py", "apps/accounts/models.py",
        ],
        cwd=BASE_DIR,
        env={},
        capture_output=True,
        text=True,
        timeout=600,
        check=False,
    )  # fmt: skip
    assert result.returncode == 0, (result.stdout + result.stderr)[-3000:]
    assert "Success" in result.stdout


# ------------------------------------------------------------------ docker-compose.yml

NO_DURABILITY = ("fsync=off", "synchronous_commit=off", "full_page_writes=off", "tmpfs")


def _services() -> dict[str, str]:
    """Each top-level service of docker-compose.yml, as the text of its block."""
    text = (BASE_DIR / "docker-compose.yml").read_text()
    body = re.search(r"^services:\n((?:(?:  .*)?\n)+)", text, flags=re.MULTILINE)
    assert body is not None
    blocks = re.split(r"^  (?=[A-Za-z][\w-]*:\n)", body.group(1), flags=re.MULTILINE)
    services = {}
    for block in blocks:
        name, _, rest = block.partition(":\n")
        if rest:
            services[name] = rest
    return services


def test_the_compose_parser_finds_every_service() -> None:
    assert set(_services()) == {"db", "db-test", "redis"}


def test_only_db_test_runs_in_memory_without_fsync() -> None:
    services = _services()
    for name, block in services.items():
        lines = [line for line in block.splitlines() if not line.lstrip().startswith("#")]
        found = [option for option in NO_DURABILITY if option in "\n".join(lines)]
        if name == "db-test":
            assert found == list(NO_DURABILITY), found
            assert "volumes:" not in block  # nothing written to disk, nothing kept
        else:
            assert found == [], f"{name}: {found}"


def test_the_development_server_keeps_its_volume_and_the_same_image() -> None:
    services = _services()
    assert re.search(r"^    volumes:\n      - pgdata:/var/lib/postgresql/data$", services["db"],
                     flags=re.MULTILINE)  # fmt: skip
    image = re.compile(r"^    image: (\S+)$", re.MULTILINE)
    assert (
        image.findall(services["db-test"])
        == image.findall(services["db"])
        == ["postgres:16-alpine"]
    )
    assert '"127.0.0.1:5433:5432"' in services["db-test"]  # local only, beside db
    assert "${TEST_POSTGRES_PASSWORD:?" in services["db-test"]


def test_env_example_documents_the_test_database() -> None:
    text = (BASE_DIR / ".env.example").read_text()
    assert re.search(r"^TEST_POSTGRES_PASSWORD=", text, flags=re.MULTILINE)
    url = re.search(r"^TEST_DATABASE_URL=(\S+)$", text, flags=re.MULTILINE)
    assert url is not None and "@127.0.0.1:5433/" in url.group(1)
