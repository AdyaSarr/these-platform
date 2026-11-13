"""Queue this week's summaries of the objectives (run every Monday by a systemd timer)."""

from __future__ import annotations

from typing import Any

from django.core.management.base import BaseCommand

from apps.objectives import summary


class Command(BaseCommand):
    help = (
        "Queue the weekly summary of the objectives to the supervisors who have something "
        "to see (dates in Paris). Idempotent: once per week and recipient."
    )

    def handle(self, *args: Any, **opts: Any) -> None:
        counts = summary.run()
        # Counts only: no title, no address on the terminal or in the journal.
        self.stdout.write(
            f"{counts['theses']} thesis(es), {counts['queued']} summary e-mail(s) queued"
        )
