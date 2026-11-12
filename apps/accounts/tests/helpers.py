"""Sign-in helpers going through allauth's real flows (not force_login)."""

from __future__ import annotations

from django.test import Client
from django.urls import reverse

from apps.accounts.models import User
from apps.accounts.tests.factories import totp_code


def password_login(client: Client, user: User, password: str) -> None:
    """First stage only: e-mail + password. The user is NOT signed in yet if MFA is on."""
    client.post(reverse("account_login"), {"login": user.email, "password": password})


def mfa_login(client: Client, user: User, password: str) -> None:
    """Full sign-in: password, then the current TOTP code."""
    password_login(client, user, password)
    response = client.post(
        reverse("mfa_authenticate"),
        {"code": totp_code(user.totp_secret)},  # type: ignore[attr-defined]
    )
    assert response.status_code == 302, response.content[:500]
    assert client.session.get("_auth_user_id") == str(user.pk)


def mfa_reauthenticate(client: Client, user: User) -> None:
    response = client.post(
        reverse("mfa_reauthenticate"),
        {"code": totp_code(user.totp_secret)},  # type: ignore[attr-defined]
    )
    assert response.status_code == 302, response.content[:500]


def password_reauthenticate(client: Client, password: str) -> None:
    response = client.post(reverse("account_reauthenticate"), {"password": password})
    assert response.status_code == 302, response.content[:500]
