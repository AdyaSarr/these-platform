"""Project-wide pytest configuration.

Fails the whole run early, with an actionable message, when PostgreSQL is not
reachable (tests run on PostgreSQL only, see docs/decisions.md).
"""

from collections.abc import Generator

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
