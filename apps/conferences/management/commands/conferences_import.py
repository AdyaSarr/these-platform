"""Import a local JSON catalogue of conferences (strict schema, dry run by default)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from django.core.exceptions import PermissionDenied, ValidationError
from django.core.management.base import BaseCommand, CommandError, CommandParser

from apps.accounts.managers import UserManager
from apps.accounts.models import Membership, Thesis
from apps.accounts.roles import Role
from apps.conferences import importer


class Command(BaseCommand):
    help = (
        "Import venues, editions and deadlines from a local JSON file (format 1, see "
        "apps/conferences/importer.py). Never reads the network; never modifies an "
        "existing row. Dry run unless --apply."
    )

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("path", type=Path)
        parser.add_argument("--thesis", required=True, help="id of the thesis")
        parser.add_argument("--as", dest="email", required=True, help="the PhD student's e-mail")
        parser.add_argument("--apply", action="store_true", help="write (default: dry run)")

    def handle(self, *args: Any, **opts: Any) -> None:
        try:
            thesis = Thesis.objects.get(pk=opts["thesis"])
        except (Thesis.DoesNotExist, ValueError, ValidationError):
            raise CommandError("unknown thesis") from None
        email = UserManager.normalize_email(opts["email"])
        membership = (
            Membership.objects.active()
            .filter(thesis=thesis, role=Role.PHD_STUDENT, user__email=email, user__is_active=True)
            .select_related("user")
            .first()
        )
        if membership is None:
            raise CommandError("--as must be the active PhD student of the thesis")
        try:
            counts = importer.run(
                opts["path"], thesis=thesis, user=membership.user, apply=opts["apply"]
            )
        except importer.ImportFileError as exc:
            raise CommandError(f"refused: {exc}") from None
        except (ValidationError, PermissionDenied) as exc:
            raise CommandError(f"refused by the platform: {type(exc).__name__}") from None
        summary = (
            f"{counts.venues} venue(s), {counts.editions} edition(s), "
            f"{counts.deadlines} deadline(s)"
        )
        if opts["apply"]:
            self.stdout.write(
                f"applied: {summary} created, {counts.conflicts} conflict(s) left untouched"
            )
        else:
            self.stdout.write(
                f"dry run: {summary} to create, {counts.conflicts} conflict(s); "
                "nothing written (--apply to write)"
            )
