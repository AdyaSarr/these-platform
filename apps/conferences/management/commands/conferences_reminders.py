"""Queue today's conference reminders (run daily by a systemd timer)."""

from __future__ import annotations

from typing import Any

from django.core.management.base import BaseCommand

from apps.conferences import reminders


class Command(BaseCommand):
    help = (
        "Queue the J-30, J-14, J-7 and J-1 reminders of the conference deadlines "
        "(dates in each deadline's zone), of the ST approval request dates (Paris) "
        "and of the approvals awaiting the tutor. Idempotent. Also queues again the "
        "failed conference e-mails."
    )

    def handle(self, *args: Any, **opts: Any) -> None:
        result = reminders.run()
        # Counts only: no title, no address on the terminal or in the journal.
        self.stdout.write(
            f"{result.deadlines} deadline reminder(s), {result.request_dues} request "
            f"reminder(s), {result.pending} pending approval reminder(s) queued, "
            f"{result.waiting} awaited approval reminder(s) queued, "
            f"{result.requeued} failed e-mail(s) queued again"
        )
