"""Recompute the audit chain and check it against checkpoints.

    manage.py audit_verify [--public-key HEX|FILE] [--checkpoints FILE]

--public-key is the key PINNED outside the platform (first export of
`audit_checkpoint --export-public-key`); the key the server computes is never
used. Without external checkpoints a truncation of the END of the chain cannot
be detected. Exit status 2 on any alteration.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from django.core.management.base import BaseCommand, CommandError, CommandParser

from apps.vault import checkpoints
from apps.vault.audit import Finding, verify_chain
from apps.vault.models import AuditCheckpoint

_KEY = re.compile(r"[0-9a-f]{64}")


def _public_key(value: str) -> bytes:
    text = value.strip()
    if not _KEY.fullmatch(text):
        path = Path(value)
        if not path.is_file():
            raise CommandError("--public-key: 64 hex digits, or a file holding them")
        text = path.read_text().strip()
        if not _KEY.fullmatch(text):
            raise CommandError(f"--public-key: {path} does not hold 64 hex digits")
    return bytes.fromhex(text)


def _external(path: Path) -> list[checkpoints.ExternalCheckpoint]:
    found = []
    for number, line in enumerate(path.read_text().splitlines(), start=1):
        if not line.strip():
            continue
        try:
            found.append(checkpoints.parse_line(line))
        except ValueError as exc:
            raise CommandError(f"{path} line {number}: {exc}") from None
    return found


class Command(BaseCommand):
    help = "Verify the audit hash chain, and checkpoints with the pinned public key."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--public-key", help="Pinned Ed25519 public key (hex, or a file).")
        parser.add_argument("--checkpoints", type=Path, help="External checkpoint export file.")

    def handle(self, *args: Any, **options: Any) -> None:
        if options["checkpoints"] is not None and not options["public_key"]:
            raise CommandError(
                "--checkpoints needs --public-key: the key pinned outside the platform "
                "(a key computed by the server is never used)"
            )
        public_key = _public_key(options["public_key"]) if options["public_key"] else None
        external: list[checkpoints.CheckpointLike] = (
            list(_external(options["checkpoints"])) if options["checkpoints"] else []
        )
        stored: list[checkpoints.CheckpointLike] = (
            list(AuditCheckpoint.objects.order_by("seq")) if public_key else []
        )
        report = verify_chain(keep_hashes_at={c.seq for c in [*external, *stored]})
        findings: list[Finding] = list(report.findings)
        if public_key is not None:
            for label, group in (("checkpoint", external), ("database checkpoint", stored)):
                findings += checkpoints.verify(
                    group,
                    public_key=public_key,
                    chain_hashes=report.hashes,
                    last_seq=report.last_seq,
                    label=label,
                )
        for finding in findings:
            self.stdout.write(f"{finding.level}\t{finding.message}")
        chain_ok = not report.altered
        self.stdout.write(f"{'OK' if chain_ok else 'CHECKED'}\t{report.events} events")
        altered = any(f.level == "ALTERATION" for f in findings)
        for label, group in (("external", external), ("database", stored)):
            if group and not altered:
                self.stdout.write(f"OK\t{len(group)} {label} checkpoint(s)")
        if not external:
            self.stdout.write(
                "WARNING\ttail truncation is not detectable without external checkpoints"
            )
        if altered:
            raise CommandError("the audit log has been altered", returncode=2)
