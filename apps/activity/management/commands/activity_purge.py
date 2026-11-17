"""Delete the activity older than 13 months (run daily)."""

from __future__ import annotations

from typing import Any

from django.core.management.base import BaseCommand

from apps.activity import services


class Command(BaseCommand):
    help = "Delete the events (with their reads and e-mails) and digests older than 13 months."

    def handle(self, *args: Any, **opts: Any) -> None:
        self.stdout.write(f"{services.purge()} row(s) deleted")
