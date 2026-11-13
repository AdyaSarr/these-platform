"""Project-wide pytest configuration.

Fails the whole run early, with an actionable message, when PostgreSQL is not
reachable (tests run on PostgreSQL only, see docs/decisions.md).
"""

from collections.abc import Generator
from pathlib import Path
from typing import Any

import pytest
from django.db import OperationalError, connections
from pytest_django import DjangoDbBlocker

DB_UNREACHABLE_MESSAGE = (
    "PostgreSQL est injoignable (DATABASE_URL). "
    "Lancez `docker compose up -d db` puis relancez pytest."
)


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
