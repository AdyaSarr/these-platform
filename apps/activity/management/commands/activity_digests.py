"""Queue the digests of « informations » (run daily at 07:00 in Paris)."""

from __future__ import annotations

from typing import Any

from django.core.management.base import BaseCommand

from apps.activity import digests


class Command(BaseCommand):
    help = (
        "Queue the daily digests (and, on Monday, the weekly ones) of the activity "
        "« informations », and the failed activity e-mails again. Idempotent."
    )

    def handle(self, *args: Any, **opts: Any) -> None:
        result = digests.run()
        # Counts only: no title, no address.
        self.stdout.write(
            f"{result['queued']} digest(s) queued, {result['again']} failed e-mail(s) queued again"
        )
