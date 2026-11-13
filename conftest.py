"""Project-wide pytest configuration.

Fails the whole run early, with an actionable message, when PostgreSQL is not
reachable (tests run on PostgreSQL only, see docs/decisions.md).
"""

import datetime as dt
import os
from collections.abc import Generator
from pathlib import Path
from typing import Any

import pytest
import time_machine
from django.db import OperationalError, connections
from pytest_django import DjangoDbBlocker

DB_UNREACHABLE_MESSAGE = (
    "PostgreSQL est injoignable (DATABASE_URL). "
    "Lancez `docker compose up -d db` puis relancez pytest."
)


@pytest.fixture(scope="session", autouse=True)
def _test_clock() -> Generator[None]:
    """Run the whole suite as if it started at ``TEST_CLOCK`` (ISO 8601 with offset).

    The clock keeps ticking from there (tick=True) and the tests' own
    time_machine travels still nest inside. Used to find tests that depend on the
    wall-clock time of the run: a date boundary (23:30 UTC, 00:30 Paris) or a run
    after the fixed instants of the tests. Unset: the real clock.
        TEST_CLOCK=2026-11-13T23:30:00+00:00 uv run pytest
    """
    value = os.environ.get("TEST_CLOCK", "").strip()
    if not value:
        yield
        return
    instant = dt.datetime.fromisoformat(value)
    if instant.tzinfo is None:
        raise pytest.UsageError("TEST_CLOCK needs an explicit offset, e.g. +00:00")
    with time_machine.travel(instant, tick=True):
        yield


@pytest.fixture(scope="session")
def django_db_modify_db_settings(
    django_db_modify_db_settings_parallel_suffix: None,
    django_db_blocker: DjangoDbBlocker,
) -> Generator[None]:
    """Runs before pytest-django creates the test database: probe the server first."""
    # pytest-django blocks database access outside explicitly allowed scopes.
    with django_db_blocker.unblock():
        try:
            connections["default"].ensure_connection()
        except OperationalError:
            pytest.exit(DB_UNREACHABLE_MESSAGE, returncode=3)
        finally:
            connections["default"].close()
    yield


@pytest.fixture(autouse=True)
def _isolated_cache() -> Generator[None]:
    """Each test starts with an empty cache: allauth keeps rate-limit counters and
    TOTP anti-replay markers there, which would otherwise leak between tests."""
    from django.core.cache import cache

    cache.clear()
    yield
    cache.clear()


@pytest.fixture(autouse=True)
def _vault_storage_in_tmp(settings: Any, tmp_path: Path) -> None:
    """Blobs written by any test go to a per-test directory, never to var/ of the repo."""
    settings.VAULT_STORAGE_DIR = str(tmp_path / "vault")
    from apps.vault import storage

    storage.init_root()  # what `manage.py vault_init` does


@pytest.fixture(autouse=True)
def _vault_keys(request: pytest.FixtureRequest) -> None:
    """Version 1 of every KEK domain, as `manage.py vault_init` creates it, for every
    test that uses the database (rows only: no audit event, so tests counting
    events are unaffected). Opt out with @pytest.mark.without_vault_keys."""
    marker = request.node.get_closest_marker("django_db")
    if request.node.get_closest_marker("without_vault_keys"):
        return
    names = set(request.fixturenames)
    if marker is None and not names & {"db", "transactional_db"}:
        return
    transactional = "transactional_db" in names or bool(marker and marker.kwargs.get("transaction"))
    request.getfixturevalue("transactional_db" if transactional else "db")
    from apps.vault.formats import KeyDomain
    from apps.vault.models import KeyEncryptionKey

    for domain in KeyDomain:
        KeyEncryptionKey.objects.get_or_create(provider="env", domain=domain, version=1)


AUDIT_TABLES = ("vault_auditevent", "vault_auditcheckpoint")


@pytest.fixture(scope="session", autouse=True)
def _flush_bypasses_only_the_audit_truncate_triggers() -> Generator[None]:
    """transaction=True tests are cleaned up with TRUNCATE, which the append-only
    audit triggers forbid. Around Django's flush ONLY, the TRUNCATE triggers of
    the two audit tables are disabled, then enabled again (test database only)."""
    from django.db.backends.postgresql.operations import DatabaseOperations

    original = DatabaseOperations.sql_flush

    def sql_flush(self: Any, style: Any, tables: Any, **kwargs: Any) -> list[str]:
        statements = original(self, style, tables, **kwargs)
        guarded = [t for t in AUDIT_TABLES if t in tables]
        if not statements or not guarded:
            return list(statements)
        off = [f'ALTER TABLE "{t}" DISABLE TRIGGER "{t}_no_truncate";' for t in guarded]
        on = [f'ALTER TABLE "{t}" ENABLE TRIGGER "{t}_no_truncate";' for t in guarded]
        return [*off, *statements, *on]

    DatabaseOperations.sql_flush = sql_flush  # type: ignore[method-assign]
    yield
    DatabaseOperations.sql_flush = original  # type: ignore[method-assign]
