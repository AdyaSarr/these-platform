"""Seal MFA secrets still stored in clear (databases from before M2: development only).

Idempotent: already sealed values are left as they are. One transaction per
authenticator, in the lock order: KEK lock (shared), row lock, then the audit
event once at the end, in its own transaction.
"""

from __future__ import annotations

from typing import Any

from django.core.management.base import BaseCommand
from django.db import transaction

from apps.vault import formats, keys, services
from apps.vault.formats import KeyDomain


class Command(BaseCommand):
    help = "Seal the MFA secrets (TOTP keys, recovery-code seeds) still stored in clear."

    def handle(self, *args: Any, **options: Any) -> None:
        from allauth.mfa.models import Authenticator

        sealed = 0
        for pk in Authenticator.objects.order_by("pk").values_list("pk", flat=True):
            with transaction.atomic():
                keys.current_kek(KeyDomain.MFA_SECRETS)  # KEK lock before the row lock
                authenticator = Authenticator.objects.select_for_update().get(pk=pk)
                data = dict(authenticator.data or {})
                clear = [
                    name
                    for name in services.MFA_SECRET_FIELDS
                    if isinstance(data.get(name), str)
                    and not data[name].startswith(formats.SEALED_TEXT_PREFIX)
                ]
                for name in clear:
                    data[name] = services.encrypt_secret(data[name])
                if clear:
                    authenticator.data = data
                    authenticator.save(update_fields=["data"])
                    sealed += len(clear)
        if sealed:
            with transaction.atomic():
                services.audit(services.AuditAction.MFA_SECRETS_SEALED, rows=sealed)
        self.stdout.write(f"{sealed} secret(s) sealed")
