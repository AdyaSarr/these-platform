"""Create a signed checkpoint of the audit chain, and copy it outside the platform.

    manage.py audit_checkpoint [--export FILE] [--email]   daily: --export; weekly: --email
    manage.py audit_checkpoint --export-public-key          print the verification key

The export file must LEAVE the server (pulled by another machine): a copy that
stays next to the chain protects nothing (docs/DEPLOYMENT.md).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from django.core.management.base import BaseCommand, CommandError, CommandParser
from django.db import transaction

from apps.vault import checkpoints, crypto, keys
from apps.vault.audit import AuditAction, audit
from apps.vault.errors import AuditError
from apps.vault.tasks import send_checkpoint_email


class Command(BaseCommand):
    help = "Sign the end of the audit chain; export it to a file and/or e-mail it."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--export", type=Path, help="Append the checkpoint to this file.")
        parser.add_argument(
            "--email", action="store_true", help="E-mail it to the active PhD student(s)."
        )
        parser.add_argument(
            "--export-public-key",
            action="store_true",
            help="Print the public key and its SHA-256 fingerprint (no checkpoint).",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        if options["export_public_key"]:
            self._export_public_key()
            return
        try:
            checkpoint = checkpoints.create_checkpoint()
        except AuditError as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(f"checkpoint seq {checkpoint.seq} hash {checkpoint.hash}")
        if options["export"] is not None:
            _append(options["export"], checkpoints.export_line(checkpoint))
            self.stdout.write(f"exported to {options['export']}")
        if options["email"]:
            send_checkpoint_email.delay(checkpoint.seq)  # the broker sees the seq only
            self.stdout.write("e-mail queued")

    def _export_public_key(self) -> None:
        public = keys.checkpoint_public_key()
        fingerprint = crypto.sha256(public).hex()
        with transaction.atomic():
            audit(AuditAction.AUDIT_PUBLIC_KEY_EXPORTED, fingerprint=fingerprint)
        self.stdout.write(f"Ed25519 public key: {public.hex()}")
        self.stdout.write(f"SHA-256 fingerprint: {fingerprint}")
        self.stdout.write(
            "Keep the key of the FIRST export outside the platform: a different key later "
            "is an alert, never a replacement (docs/SECURITY.md)."
        )


def _append(path: Path, line: str) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        os.fchmod(fd, 0o600)
        os.write(fd, (line + "\n").encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)
