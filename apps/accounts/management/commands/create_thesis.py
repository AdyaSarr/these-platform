"""Create a thesis and invite its PhD student: the platform's only entry point."""

from __future__ import annotations

import datetime as dt
from typing import Any

from django.core.management.base import BaseCommand, CommandError, CommandParser

from apps.accounts import services
from apps.accounts.models import Organization


def _date(value: str) -> dt.date:
    try:
        return dt.date.fromisoformat(value)
    except ValueError as exc:
        raise CommandError(f"invalid date {value!r} (expected YYYY-MM-DD)") from exc


class Command(BaseCommand):
    help = "Create a thesis and e-mail an invitation to its PhD student."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--title", required=True)
        parser.add_argument("--cifre-start", required=True, type=_date)
        parser.add_argument("--first-enrollment", required=True, type=_date)
        parser.add_argument("--planned-end", required=True, type=_date)
        parser.add_argument("--student-email", required=True)
        parser.add_argument("--student-organization", default="STMicroelectronics")
        parser.add_argument(
            "--student-organization-kind",
            default=Organization.Kind.ST,
            choices=Organization.Kind.values,
        )

    def handle(self, *args: Any, **opts: Any) -> None:
        try:
            inv = services.create_thesis(
                title=opts["title"],
                cifre_start=opts["cifre_start"],
                first_enrollment=opts["first_enrollment"],
                planned_end=opts["planned_end"],
                student_email=opts["student_email"],
                student_organization=opts["student_organization"],
                student_organization_kind=opts["student_organization_kind"],
            )
        except (services.ThesisCreationError, services.TeamError) as exc:
            raise CommandError(str(exc)) from exc
        if inv.email_status != inv.EmailStatus.SENT:
            raise CommandError("Thesis created, but the invitation e-mail could not be sent.")
        self.stdout.write(self.style.SUCCESS(f"Thesis created; invitation sent to {inv.email}."))
