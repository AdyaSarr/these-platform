"""demo_db: drops and creates a database ending with _demo, never anything else."""

import io
import secrets
from typing import Any

import pytest
from django.core.management import CommandError, call_command

from apps.demo import database


@pytest.mark.parametrize(
    "name",
    ["theses", "test_theses", "theses_demo_old", "postgres", "_demo", "x_demo; DROP", "Theses_demo",
     "a" * 60 + "_demo", ""],
)  # fmt: skip
def test_only_demo_names_are_accepted(name: str) -> None:
    with pytest.raises(ValueError, match="_demo"):
        database.checked_name(name)
    with pytest.raises(ValueError):
        database.drop(name)


def test_demo_names_are_accepted() -> None:
    assert database.checked_name("theses_demo") == "theses_demo"


@pytest.mark.django_db
def test_create_then_drop_on_the_server() -> None:
    """A throwaway database: created, seen in pg_database, dropped."""
    name = f"pytest_{secrets.token_hex(4)}_demo"
    assert not database.exists(name)
    database.create(name)
    try:
        assert database.exists(name)
    finally:
        database.drop(name)
    assert not database.exists(name)


@pytest.mark.parametrize("action", ["describe", "drop", "create"])
def test_command_refused_outside_demo_settings(settings: Any, action: str) -> None:
    settings.DEBUG = True  # test settings: demo_seed may run, demo_db may not
    with pytest.raises(CommandError, match=r"config\.settings\.demo"):
        call_command("demo_db", action, stdout=io.StringIO())
