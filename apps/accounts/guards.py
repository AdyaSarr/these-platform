"""Start-up guards that need the app registry (called from config/wsgi.py, asgi.py)."""

from __future__ import annotations

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured


def require_mfa_encryption() -> None:
    """Refuse to serve when MFA secrets would be stored in clear (M1 -> M2).

    Checks the behaviour, not a flag: encrypt() of a probe must differ from the
    probe and decrypt back to it.
    """
    if not settings.MFA_ENCRYPTION_REQUIRED:
        return
    from allauth.mfa.adapter import get_adapter

    adapter = get_adapter()
    probe = "mfa-encryption-probe"
    encrypted = adapter.encrypt(probe)
    if encrypted == probe or adapter.decrypt(encrypted) != probe:
        raise ImproperlyConfigured(
            "MFA secrets (TOTP keys, recovery-code seeds) would be stored in clear: "
            "MFAAdapter.encrypt() must use the vault (M2) before production."
        )
