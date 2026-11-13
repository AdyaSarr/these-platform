"""Prepare the vault: storage directory (mode 700) and version 1 of every KEK domain.

Run once per environment, before the first upload; safe to run again. Refuses
an existing directory with wrong owner or permissions, or a symbolic link.
Creating the KEK versions here keeps their audit events out of business
transactions (vault.keys.current_kek never creates a version).
"""

from typing import Any

from django.core.management.base import BaseCommand, CommandError

from apps.vault import keys, storage
from apps.vault.errors import StorageError, VaultKeyError


class Command(BaseCommand):
    help = "Create the vault storage directory and the first KEK version of every domain."

    def handle(self, *args: Any, **options: Any) -> None:
        try:
            created = storage.init_root()
            new_keks = keys.init_domains()
        except (StorageError, VaultKeyError) as exc:
            raise CommandError(str(exc)) from exc
        state = "created" if created else "already present, permissions OK"
        self.stdout.write(f"{storage.root()}: {state}")
        for kek_id in new_keks:
            self.stdout.write(f"KEK created: {kek_id}")
        if not new_keks:
            self.stdout.write("KEKs: every domain already has a version")
