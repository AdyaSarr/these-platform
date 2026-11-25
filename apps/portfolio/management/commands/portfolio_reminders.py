"""Queue today's re-registration reminders (run daily by a systemd timer)."""

from __future__ import annotations

from typing import Any

from django.core.management.base import BaseCommand

from apps.portfolio import reminders


class Command(BaseCommand):
    help = (
        "Queue the reminders of the next re-registration (30 and 7 days before, dates "
        "in Paris). Idempotent. Also queues again the failed portfolio e-mails."
    )

    def handle(self, *args: Any, **opts: Any) -> None:
        result = reminders.run()
        # Counts only: no date, no address.
        self.stdout.write(
            f"{result.reminders} re-registration reminder(s) queued, "
            f"{result.requeued} failed e-mail(s) queued again"
        )
