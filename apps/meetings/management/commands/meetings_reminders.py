"""Queue today's meeting e-mails (run daily by a systemd timer)."""

from __future__ import annotations

from typing import Any

from django.core.management.base import BaseCommand

from apps.meetings import reminders


class Command(BaseCommand):
    help = (
        "Queue the reminders of tomorrow's meetings and the overdue actions of "
        "yesterday (dates in Paris). Idempotent: running it again, or twice at "
        "once, sends nothing twice. Also queues again the failed meeting e-mails."
    )

    def handle(self, *args: Any, **opts: Any) -> None:
        result = reminders.run()
        # Counts only: no title, no address on the terminal or in the journal.
        self.stdout.write(
            f"{result.reminders} meeting reminder(s), {result.overdue} overdue action(s) "
            f"queued, {result.requeued} failed e-mail(s) queued again"
        )
