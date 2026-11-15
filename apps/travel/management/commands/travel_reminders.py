"""Queue today's travel reminders (run daily by a systemd timer)."""

from __future__ import annotations

from typing import Any

from django.core.management.base import BaseCommand

from apps.travel import reminders


class Command(BaseCommand):
    help = (
        "Queue the reminders of the mission approvals to request and awaiting a decision, "
        "and of the missing receipts (dates in Paris). Idempotent. Also queues again the "
        "failed travel e-mails."
    )

    def handle(self, *args: Any, **opts: Any) -> None:
        result = reminders.run()
        # Counts only: no destination, no amount, no address.
        self.stdout.write(
            f"{result.to_request} approval to request reminder(s), {result.pending} pending "
            f"approval reminder(s), {result.receipts} missing receipt reminder(s) queued, "
            f"{result.requeued} failed e-mail(s) queued again"
        )
