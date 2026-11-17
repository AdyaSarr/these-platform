"""Revoke the expired feeds and those of finished theses; warn 14 days before
expiry (run daily by a systemd timer)."""

from __future__ import annotations

from typing import Any

from django.core.management.base import BaseCommand

from apps.feeds import services


class Command(BaseCommand):
    help = "Revoke the expired iCal feeds and those of finished theses; warn before expiry."

    def handle(self, *args: Any, **opts: Any) -> None:
        counts = services.daily()
        # Counts only: no token, no address.
        self.stdout.write(
            f"{counts['expired']} expired, {counts['closed']} of a finished thesis, "
            f"{counts['notices']} notice(s) queued"
        )
