"""Create the missing catalogue items of existing theses (idempotent)."""

from __future__ import annotations

import uuid
from typing import Any

from django.core.management.base import BaseCommand, CommandError, CommandParser

from apps.accounts.models import Thesis
from apps.accounts.statuses import open_statuses
from apps.admin_file.services import seed_items

OPEN_STATUSES = open_statuses("admin_file")  # apps/accounts/statuses.py


def _uuid(value: str) -> uuid.UUID:
    try:
        return uuid.UUID(value)
    except ValueError as exc:
        raise CommandError(f"invalid thesis id {value!r}") from exc


class Command(BaseCommand):
    help = (
        "Create the administrative-file items of the catalogue missing from a thesis "
        "(--thesis) or from every open thesis (--all). Existing items are never modified."
    )

    def add_arguments(self, parser: CommandParser) -> None:
        target = parser.add_mutually_exclusive_group(required=True)
        target.add_argument("--thesis", type=_uuid)
        target.add_argument("--all", action="store_true")

    def handle(self, *args: Any, **opts: Any) -> None:
        if opts["all"]:
            theses = list(Thesis.objects.filter(status__in=OPEN_STATUSES).order_by("created_at"))
        else:
            thesis = Thesis.objects.filter(pk=opts["thesis"]).first()
            if thesis is None:
                raise CommandError("No such thesis.")
            if thesis.status not in OPEN_STATUSES:
                raise CommandError("The thesis is defended or closed: its file is frozen.")
            theses = [thesis]
        for thesis in theses:
            created = seed_items(thesis)
            # The id only: the thesis title stays off the terminal and its logs.
            self.stdout.write(f"{thesis.pk}: {len(created)} item(s) created {created}")
