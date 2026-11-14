"""Create the demonstration thesis on a fresh demonstration database.

    DJANGO_SETTINGS_MODULE=config.settings.demo uv run python manage.py demo_seed

Normally run by scripts/demo_reset.sh. Refused outside config.settings.demo (and
the tests), with DEBUG off, or on a database whose name does not end with _demo.
No option. The links printed at the end are shown ONCE, on standard output only.
"""

from __future__ import annotations

from typing import Any

from django.core.management.base import BaseCommand, CommandError

from apps.demo import guard, scenario


class Command(BaseCommand):
    help = "Create the demonstration thesis (demonstration database only)."

    def handle(self, *args: Any, **options: Any) -> None:
        refused = guard.current_refusal()
        if refused is not None:
            raise CommandError(f"demo_seed refused: {refused}")
        try:
            result = scenario.seed()
        except scenario.ScenarioError as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(self.style.SUCCESS(f"Demonstration thesis created ({result.thesis_id})."))
        self.stdout.write(
            "\nOne link per person, valid a few days, shown only now. Each person sets a\n"
            "password, signs in, then enrolls a second factor. Afterwards: "
            "« Mot de passe oublié ».\n"
        )
        for link in result.links:
            self.stdout.write(f"{link.role.label} — {link.name} <{link.email}>")
            self.stdout.write(f"    {link.url}")
