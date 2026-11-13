"""Start-up guards that need the app registry (called from config/wsgi.py, asgi.py)."""

from __future__ import annotations

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured


def require_mfa_encryption() -> None:
    """Refuse to serve unless MFA secrets are sealed by the vault (M2).

    Structural, WITHOUT the database (it runs when config.wsgi is imported, and
    the database may not be up yet): the configured adapter is
    accounts.adapters.MFAAdapter, or a subclass redefining neither encrypt()
    nor decrypt(), and those methods call vault.services. A behaviour probe
    would accept any reversible encoding and would need the database. The real
    round trip is tested with a database, and checked on the deployed one by
    `manage.py check --deploy --database default` (vault.E001).
    """
    if not settings.MFA_ENCRYPTION_REQUIRED:
        return
    from allauth.mfa.adapter import get_adapter

    from .adapters import MFAAdapter

    adapter = get_adapter()
    cls = type(adapter)
    sealed_by_the_vault = (
        isinstance(adapter, MFAAdapter)
        and cls.encrypt is MFAAdapter.encrypt
        and cls.decrypt is MFAAdapter.decrypt
        and "encrypt_secret" in MFAAdapter.encrypt.__code__.co_names
        and "decrypt_secret" in MFAAdapter.decrypt.__code__.co_names
    )
    if not sealed_by_the_vault:
        raise ImproperlyConfigured(
            "MFA secrets (TOTP keys, recovery-code seeds) must be sealed by the vault: "
            "MFA_ADAPTER must be apps.accounts.adapters.MFAAdapter, with encrypt() and "
            "decrypt() not redefined."
        )
