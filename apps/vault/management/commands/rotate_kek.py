"""Rotate the KEK of one key domain, in two steps (docs/SECURITY.md):

    manage.py rotate_kek <DOMAIN>            new KEK version, then every row re-wrapped
    manage.py rotate_kek <DOMAIN> --retire   later: re-wrap stragglers, retire old versions

The second step is refused until the new version is VAULT_RETIRE_GRACE old
(10 min): data sealed outside the shared lock (allauth's encrypt(), open()'s
read window) may still be stored under the old version. Each batch is its own
short transaction. A busy exclusive lock (KekLockTimeoutError) is retried with
a growing delay.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from functools import partial
from typing import Any

from django.core.management.base import BaseCommand, CommandError, CommandParser

from apps.vault import keys, services
from apps.vault.errors import KekLockTimeoutError, KekRetirementError, VaultKeyError
from apps.vault.formats import KeyDomain
from apps.vault.models import KeyEncryptionKey

RETRY_DELAYS = (1, 2, 4, 8)  # seconds between the 5 attempts
_sleep = time.sleep  # replaced in tests


def _with_retries[T](operation: Callable[[], T]) -> T:
    for delay in (*RETRY_DELAYS, None):
        try:
            return operation()
        except KekLockTimeoutError:
            if delay is None:
                raise
            _sleep(delay)
    raise AssertionError("unreachable")


class Command(BaseCommand):
    help = "Rotate the KEK of a key domain (then, after the grace period, --retire)."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("domain", choices=[d.value for d in KeyDomain])
        parser.add_argument("--batch", type=int, default=100, help="Rows per transaction (100).")
        parser.add_argument(
            "--retire",
            action="store_true",
            help="Re-wrap what is left, then retire every non-current version.",
        )

    def handle(self, *args: Any, domain: str, batch: int, retire: bool, **options: Any) -> None:
        key_domain = KeyDomain(domain)
        try:
            if retire:
                self._retire(key_domain, batch)
            else:
                kek_id = _with_retries(lambda: keys.new_version(key_domain))
                self.stdout.write(f"new version {kek_id}")
                self._rewrap(key_domain, batch)
                self.stdout.write(
                    f"run `manage.py rotate_kek {domain} --retire` once the grace period "
                    "(VAULT_RETIRE_GRACE) has passed"
                )
        except (KekLockTimeoutError, KekRetirementError, VaultKeyError) as exc:
            raise CommandError(str(exc)) from exc

    def _rewrap(self, domain: KeyDomain, batch: int) -> None:
        total = 0
        while count := services.rewrap_batch(domain, limit=batch):
            total += count
        self.stdout.write(f"re-wrapped {total} row(s)")

    def _retire(self, domain: KeyDomain, batch: int) -> None:
        self._rewrap(domain, batch)  # rows sealed late under an old version
        old = (
            KeyEncryptionKey.objects.filter(domain=domain, retired_at__isnull=True)
            .order_by("version")
            .values_list("kek_id", flat=True)
        )
        for kek_id in list(old)[:-1]:  # every version but the current one
            _with_retries(partial(keys.retire, kek_id))
            self.stdout.write(f"retired {kek_id}")
