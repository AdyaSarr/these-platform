"""Create a thesis and invite its PhD student: the platform's only entry point."""

from __future__ import annotations

import datetime as dt
from typing import Any

from django.core.management.base import BaseCommand, CommandError, CommandParser
from django.db import transaction

from apps.accounts import services, tasks
from apps.accounts.models import Organization, Thesis
from apps.vault.services import AuditAction, audit, vault_problems


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
        # The student's first sign-in seals a TOTP secret with the vault: it must
        # be initialized before any invitation goes out.
        problems = vault_problems()
        if problems:
            raise CommandError(
                "The vault is not initialized (" + "; ".join(problems) + "): "
                "run `manage.py vault_init` first."
            )
        if opts["planned_end"] <= opts["cifre_start"]:
            raise CommandError("--planned-end must be after --cifre-start.")
        with transaction.atomic():
            org, _ = Organization.objects.get_or_create(
                name=opts["student_organization"],
                defaults={"kind": opts["student_organization_kind"]},
            )
            thesis = Thesis.objects.create(
                title=opts["title"],
                cifre_start_date=opts["cifre_start"],
                first_enrollment_date=opts["first_enrollment"],
                planned_end_date=opts["planned_end"],
            )
            audit(AuditAction.THESIS_CREATED, obj=thesis)
            try:
                inv = services.invite_phd_student(
                    thesis=thesis, email=opts["student_email"], organization=org
                )
            except services.TeamError as exc:  # rolls the thesis back too
                raise CommandError(str(exc)) from exc
        # Sent synchronously: the command works without a Celery worker.
        tasks.send_invitation_email(str(inv.pk))
        inv.refresh_from_db()
        if inv.email_status != inv.EmailStatus.SENT:
            raise CommandError("Thesis created, but the invitation e-mail could not be sent.")
        self.stdout.write(self.style.SUCCESS(f"Thesis created; invitation sent to {inv.email}."))
