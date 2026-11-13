"""Create the vault storage directory (VAULT_STORAGE_DIR) in mode 700.

Run once per environment, before the first upload; safe to run again. Refuses
an existing directory with wrong owner or permissions, or a symbolic link.
"""

from typing import Any

from django.core.management.base import BaseCommand, CommandError

from apps.vault import storage
from apps.vault.errors import StorageError


class Command(BaseCommand):
    help = "Create the vault storage directory (mode 700); refuse unsafe existing ones."

    def handle(self, *args: Any, **options: Any) -> None:
        try:
            created = storage.init_root()
        except StorageError as exc:
            raise CommandError(str(exc)) from exc
        state = "created" if created else "already present, permissions OK"
        self.stdout.write(f"{storage.root()}: {state}")
