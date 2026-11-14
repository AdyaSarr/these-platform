"""Where demo_seed may run: a demonstration database on a local machine, nothing else.

No option bypasses these checks. The app is moreover installed only by
config.settings.demo and config.settings.test.
"""

from __future__ import annotations

import os

from django.conf import settings

DEMO_SETTINGS = "config.settings.demo"
TEST_SETTINGS = "config.settings.test"
DEMO_SUFFIX = "_demo"


def refusal(
    *, debug: bool, settings_module: str, database_name: str, celery_eager: bool
) -> str | None:
    """Why demo_seed must not run with these settings; None when it may."""
    if debug is not True:
        return "DEBUG is False: demo_seed runs on a local demonstration machine only."
    if settings_module.endswith(".prod") or ".prod." in settings_module:
        return f"refused under production settings ({settings_module})."
    if settings_module == TEST_SETTINGS:
        if not database_name.startswith("test_"):
            return "under test settings, the database name must start with test_."
    elif settings_module == DEMO_SETTINGS:
        if not database_name.endswith(DEMO_SUFFIX):
            return f"the database name must end with {DEMO_SUFFIX} (got {database_name!r})."
    else:
        return f"run with DJANGO_SETTINGS_MODULE={DEMO_SETTINGS} (got {settings_module!r})."
    if celery_eager is not True:
        # Otherwise an invitation would go through the broker to a worker that
        # e-mails it for real (dev: to the console, token included).
        return "CELERY_TASK_ALWAYS_EAGER must be True (config.settings.demo sets it)."
    return None


def current_refusal() -> str | None:
    from config.celery import app

    # An override_settings() layer hides SETTINGS_MODULE (None): the module Django
    # was loaded from is then the environment variable it read at start-up.
    module = getattr(settings, "SETTINGS_MODULE", None) or os.environ.get(
        "DJANGO_SETTINGS_MODULE", ""
    )
    return refusal(
        debug=settings.DEBUG,
        settings_module=module,
        database_name=str(settings.DATABASES["default"]["NAME"]),
        celery_eager=bool(app.conf.task_always_eager),
    )
