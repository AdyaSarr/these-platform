"""allauth adapters: our policy plugged into allauth's flows."""

from __future__ import annotations

from typing import Any

from allauth.account.adapter import DefaultAccountAdapter
from allauth.mfa.adapter import DefaultMFAAdapter
from django.contrib.auth.base_user import AbstractBaseUser
from django.db import transaction
from django.http import HttpRequest

from . import tasks


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
    # The TOTP secret and the recovery-code seed go through encrypt()/decrypt().
    # M1: identity, so production refuses to start (MFA_ENCRYPTION_REQUIRED,
    # config/wsgi.py). M2 replaces both with AEAD under a vault KEK.
    ENCRYPTS_SECRETS = False

    def encrypt(self, text: str) -> str:
        return text

    def decrypt(self, encrypted_text: str) -> str:
        return encrypted_text
