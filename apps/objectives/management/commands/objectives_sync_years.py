"""Create or update the ThesisYear rows from the thesis dates (idempotent).

New theses get their years by themselves (post_save of Thesis); this command
covers the theses created before M4, and any repair: --all or --thesis <id>.
"""

from __future__ import annotations

import uuid
from typing import Any

from django.core.management.base import BaseCommand, CommandError, CommandParser

from apps.accounts.models import Thesis
from apps.objectives.services import sync_years


class Command(BaseCommand):
    help = "Create or update the CIFRE and academic years of the theses. Idempotent."

    def add_arguments(self, parser: CommandParser) -> None:
        group = parser.add_mutually_exclusive_group(required=True)
        group.add_argument("--all", action="store_true", help="every thesis")
        group.add_argument("--thesis", metavar="UUID", help="one thesis, by id")

    def handle(self, *args: Any, **opts: Any) -> None:
        if opts["all"]:
            theses = list(Thesis.objects.order_by("pk"))
        else:
            try:
                thesis_id = uuid.UUID(opts["thesis"])
            except ValueError:
                raise CommandError("--thesis expects a UUID") from None
            theses = list(Thesis.objects.filter(pk=thesis_id))
            if not theses:
                raise CommandError(f"no thesis {thesis_id}")
        totals = [0, 0, 0, 0]
        for thesis in theses:
            result = sync_years(thesis)
            totals = [a + b for a, b in zip(totals, result, strict=True)]
            if result.kept:
                # Ids only: thesis titles stay off the terminal and the journal.
                self.stdout.write(
                    f"thesis {thesis.pk}: {result.kept} year(s) past the planned end kept "
                    "(objectives point to them)"
                )
        created, updated, deleted, kept = totals
        self.stdout.write(
            f"{len(theses)} thesis(es): {created} created, {updated} updated, "
            f"{deleted} deleted, {kept} kept"
        )
