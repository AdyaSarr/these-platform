"""Sign-in helpers going through allauth's real flows (not force_login)."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

import time_machine
from django.test import Client
from django.urls import reverse

from apps.accounts.models import User
from apps.accounts.tests.factories import totp_code

if TYPE_CHECKING:  # defined in django-stubs only
    from django.test.client import _MonkeyPatchedWSGIResponse as TestResponse


def password_login(client: Client, user: User, password: str) -> None:
    """First stage only: e-mail + password. The user is NOT signed in yet if MFA is on."""
    client.post(reverse("account_login"), {"login": user.email, "password": password})


def submit_totp(client: Client, path: str, secret: str, **data: str) -> TestResponse:
    """POST the current TOTP code of ``secret`` to ``path``, at ONE instant.

    The code is computed here and checked by allauth in the view, each reading
    the clock; with MFA_TOTP_TOLERANCE = 0, a 30-second boundary falling
    between the two reads made the sign-in fail at random. The clock is frozen
    (at the current, possibly travelled, time) for the computation and the
    request, then released. A test posting a code on a running clock goes through
    here; computing it under its own tick=False travel is safe too.
    """
    with time_machine.travel(time.time(), tick=False):
        response = client.post(path, {"code": totp_code(secret), **data})
    return response


def mfa_login(client: Client, user: User, password: str) -> None:
    """Full sign-in: password, then the current TOTP code."""
    password_login(client, user, password)
    response = submit_totp(client, reverse("mfa_authenticate"), user.totp_secret)  # type: ignore[attr-defined]
    assert response.status_code == 302, response.content[:500]
    assert client.session.get("_auth_user_id") == str(user.pk)


def mfa_reauthenticate(client: Client, user: User) -> None:
    response = submit_totp(client, reverse("mfa_reauthenticate"), user.totp_secret)  # type: ignore[attr-defined]
    assert response.status_code == 302, response.content[:500]


def password_reauthenticate(client: Client, password: str) -> None:
    response = client.post(reverse("account_reauthenticate"), {"password": password})
    assert response.status_code == 302, response.content[:500]
