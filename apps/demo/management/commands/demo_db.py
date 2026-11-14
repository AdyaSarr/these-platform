"""Administration of the demonstration database, called by scripts/demo_reset.sh.

    demo_db describe   database=<name> and vault=<directory>, what the script deletes
    demo_db drop       DROP DATABASE <name>_demo (refused for any other name)
    demo_db create     CREATE DATABASE <name>_demo

Under config.settings.demo only (DEBUG on, name ending with _demo).
"""

from __future__ import annotations

from typing import Any

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError, CommandParser

from apps.demo import database, guard


class Command(BaseCommand):
    help = "Describe, drop or create the demonstration database (config.settings.demo only)."
    # The database may not exist yet (create): no system check touches it.
    requires_system_checks = ()

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("action", choices=["describe", "drop", "create"])

    def handle(self, *args: Any, **options: Any) -> None:
        refused = guard.current_refusal()
        if refused is None and getattr(settings, "SETTINGS_MODULE", "") != guard.DEMO_SETTINGS:
            refused = f"run with DJANGO_SETTINGS_MODULE={guard.DEMO_SETTINGS}."
        if refused is not None:
            raise CommandError(f"demo_db refused: {refused}")
        try:
            name = database.checked_name(str(settings.DATABASES["default"]["NAME"]))
        except ValueError as exc:
            raise CommandError(str(exc)) from exc
        if options["action"] == "describe":
            self.stdout.write(f"database={name}")
            self.stdout.write(f"vault={settings.VAULT_STORAGE_DIR}")
        elif options["action"] == "drop":
            database.drop(name)
            self.stdout.write(f"dropped {name}")
        else:
            database.create(name)
            self.stdout.write(f"created {name}")
