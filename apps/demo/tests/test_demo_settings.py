"""config/settings/demo.py, checked on its source and on what it computes
(imported in a subprocess: it reads the developer's environment)."""

import json
import os
import subprocess  # nosec B404
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]

PROBE = """
import json
from django.conf import settings
import django
django.setup()
from config.celery import app
print(json.dumps({
    "name": settings.DATABASES["default"]["NAME"],
    "vault": settings.VAULT_STORAGE_DIR,
    "debug": settings.DEBUG,
    "eager": bool(app.conf.task_always_eager),
    "demo": "apps.demo" in settings.INSTALLED_APPS,
}))
"""


@pytest.fixture(scope="module")
def demo_settings() -> dict[str, object]:
    env = {**os.environ, "DJANGO_SETTINGS_MODULE": "config.settings.demo"}
    env.pop("TEST_CLOCK", None)
    result = subprocess.run(  # nosec B603
        [sys.executable, "-c", PROBE],
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr[-2000:]
    value: dict[str, object] = json.loads(result.stdout.strip().splitlines()[-1])
    return value


def test_database_name_ends_with_demo(demo_settings: dict[str, object]) -> None:
    name = demo_settings["name"]
    assert isinstance(name, str) and name.endswith("_demo")
    assert not name.endswith("_demo_demo")


def test_vault_directory_is_the_one_the_script_deletes(demo_settings: dict[str, object]) -> None:
    assert demo_settings["vault"] == str(REPO / "var" / "vault-demo")


def test_debug_eager_celery_and_demo_app(demo_settings: dict[str, object]) -> None:
    assert demo_settings["debug"] is True
    assert demo_settings["eager"] is True
    assert demo_settings["demo"] is True
