"""Queue today's reminders of the administrative file (run daily by a systemd timer)."""

from __future__ import annotations

from typing import Any

from django.core.management.base import BaseCommand

from apps.admin_file import reminders


class Command(BaseCommand):
    help = (
        "Queue the J-14, J-3 and overdue reminders of the day (dates in Paris). "
        "Idempotent: running it again, or twice at once, sends nothing twice."
    )

    def handle(self, *args: Any, **opts: Any) -> None:
        counts = reminders.run()
        # Counts only: no label, no code, no address on the terminal or in the journal.
        self.stdout.write(
            f"{counts['items']} item(s) in a reminder window, {counts['queued']} e-mail(s) queued"
        )
