"""Acceptance: migrations apply and none is missing for the current models."""

import pytest
from django.core.management import call_command


@pytest.mark.django_db
def test_no_missing_migration() -> None:
    # Exits with status 1 if a model change has no migration.
    call_command("makemigrations", "--check", "--dry-run", verbosity=0)


@pytest.mark.django_db
def test_user_table_exists_from_first_migration() -> None:
    from django.db import connection

    assert "accounts_user" in connection.introspection.table_names()
