"""demo_seed runs on a demonstration database only: DEBUG, demo settings, a
`_demo` database, eager Celery. No option bypasses the guard."""

import io
from pathlib import Path
from typing import Any

import pytest
from django.core.management import CommandError, call_command, get_commands, load_command_class

from apps.accounts.models import Thesis, User
from apps.demo import guard
from apps.vault.models import AuditEvent

GOOD: dict[str, Any] = {
    "debug": True,
    "settings_module": "config.settings.demo",
    "database_name": "theses_demo",
    "celery_eager": True,
}


def test_demo_settings_are_accepted() -> None:
    assert guard.refusal(**GOOD) is None
    assert guard.refusal(**{**GOOD, "settings_module": "config.settings.test",
                            "database_name": "test_theses"}) is None  # fmt: skip


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ({"debug": False}, "DEBUG"),
        ({"settings_module": "config.settings.prod"}, "prod"),
        ({"settings_module": "config.settings.dev"}, "config.settings.demo"),
        ({"settings_module": "somewhere.prod"}, "prod"),
        ({"settings_module": ""}, "config.settings.demo"),
        ({"database_name": "theses"}, "_demo"),
        ({"database_name": "theses_demo_old"}, "_demo"),
        ({"database_name": ""}, "_demo"),
        ({"settings_module": "config.settings.test", "database_name": "theses"}, "test_"),
        ({"celery_eager": False}, "CELERY_TASK_ALWAYS_EAGER"),
    ],
)
def test_anything_else_is_refused(change: dict[str, Any], reason: str) -> None:
    refused = guard.refusal(**{**GOOD, **change})
    assert refused is not None
    assert reason in refused


@pytest.mark.django_db
def test_command_refuses_without_debug_and_writes_nothing(settings: Any) -> None:
    settings.DEBUG = False  # pytest-django's default; stated for clarity
    with pytest.raises(CommandError, match="DEBUG"):
        call_command("demo_seed", stdout=io.StringIO())
    assert not Thesis.objects.exists()
    assert not User.objects.exists()
    assert not AuditEvent.objects.exists()


@pytest.mark.django_db
def test_command_refuses_under_prod_settings(settings: Any) -> None:
    settings.DEBUG = True
    settings.SETTINGS_MODULE = "config.settings.prod"
    with pytest.raises(CommandError, match="prod"):
        call_command("demo_seed", stdout=io.StringIO())
    assert not AuditEvent.objects.exists()


def test_command_has_no_option_at_all() -> None:
    """Not even a --force: the only options are Django's own."""
    command = load_command_class("apps.demo", "demo_seed")
    parser = command.create_parser("manage.py", "demo_seed")
    own = {a.dest for a in parser._actions} - {
        "help", "version", "verbosity", "settings", "pythonpath", "traceback",
        "no_color", "force_color", "skip_checks",
    }  # fmt: skip
    assert own == set()


def test_demo_app_is_absent_outside_demo_and_test_settings() -> None:
    root = Path(__file__).resolve().parents[3] / "config" / "settings"
    for name in ("base.py", "dev.py", "prod.py"):
        assert "apps.demo" not in (root / name).read_text(), name
    assert "apps.demo" in (root / "demo.py").read_text()
    from config.settings import base

    assert "apps.demo" not in base.INSTALLED_APPS


def test_command_is_discovered_in_test_settings() -> None:
    assert get_commands()["demo_seed"] == "apps.demo"
