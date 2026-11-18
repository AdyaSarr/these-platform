"""Daily run of the end of the thesis (M13a): expire the closure requests past 14
days, end the memberships due on CLOSED theses. Prints counts only."""

from __future__ import annotations

from typing import Any

from django.core.management.base import BaseCommand

from apps.lifecycle import services


class Command(BaseCommand):
    help = "Expire closure requests; end the memberships due on closed theses."

    def handle(self, *args: Any, **options: Any) -> None:
        counts = services.daily()
        self.stdout.write(" ".join(f"{key}={value}" for key, value in sorted(counts.items())))
