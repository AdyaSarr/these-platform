"""allauth adapters: our policy plugged into allauth's flows."""

from __future__ import annotations

import logging
from typing import Any

from allauth.account.adapter import DefaultAccountAdapter
from allauth.mfa.adapter import DefaultMFAAdapter
from django.contrib.auth.base_user import AbstractBaseUser
from django.db import transaction
from django.http import HttpRequest

from apps.vault import services as vault_services

from . import qr, tasks

logger = logging.getLogger(__name__)


class AccountAdapter(DefaultAccountAdapter):
    def is_open_for_signup(self, request: HttpRequest) -> bool:
        """No public sign-up: accounts are created by accepting an invitation."""
        return False

    def send_password_reset_mail(
        self, user: AbstractBaseUser, email: str, context: dict[str, Any]
    ) -> None:
        """Hand the e-mail to Celery with the user id only.

        The worker builds the reset key and link itself, so no secret goes
        through the broker. Sending in the background also keeps the response
        time independent of the address being known (anti-enumeration).
        """
        user_id = str(user.pk)
        transaction.on_commit(lambda: tasks.send_password_reset_email.delay(user_id))


class MFAAdapter(DefaultMFAAdapter):
    """The TOTP key and the recovery-code seed are sealed by the vault (M2): AES-256-GCM
    under the MFA_SECRETS KEK. A clear or malformed stored value is refused.

    The production guard (apps/accounts/guards.py) accepts only this class, or a
    subclass that redefines neither method.
    """

    def encrypt(self, text: str) -> str:
        return vault_services.encrypt_secret(text)

    def decrypt(self, encrypted_text: str) -> str:
        return vault_services.decrypt_secret(encrypted_text)

    def build_totp_svg(self, url: str) -> str:
        """The QR code as inline SVG (CSP: no data: image), or "" if it fails the
        allow-list (apps/accounts/qr.py): the page then offers the secret field only.
        Never logs the SVG or the URL, which hold the secret."""
        try:
            return qr.inline_svg(super().build_totp_svg(url))
        except ValueError:
            logger.warning("TOTP QR code refused by the SVG allow-list; secret field only")
            return ""
