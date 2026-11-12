"""Strict CSP (no inline script or style, docs/decisions.md) on the pages we did not
write ourselves: allauth's sign-in and MFA templates, rendered for real."""

import datetime as dt
import re
import secrets

import pytest
import time_machine
from allauth.mfa.recovery_codes.internal.auth import RecoveryCodes
from django.test import Client
from django.urls import reverse

from apps.accounts.tests.factories import UserFactory
from apps.accounts.tests.helpers import mfa_login, mfa_reauthenticate, password_login

PASSWORD = secrets.token_urlsafe(16)
T0 = dt.datetime(2026, 12, 1, 10, 0, tzinfo=dt.UTC)
SCRIPT = re.compile(r"<script\b([^>]*)>", re.IGNORECASE)
INLINE_ATTR = re.compile(r"""\s(style|on[a-z]+)\s*=\s*["']""", re.IGNORECASE)


def _assert_csp_clean(html: str, page: str) -> None:
    for attrs in SCRIPT.findall(html):
        # External scripts are fine ('self'); inline ones only as inert JSON data.
        assert "src=" in attrs or 'type="application/json"' in attrs, (page, attrs)
    found = INLINE_ATTR.findall(html)
    assert not found, (page, found)


ANONYMOUS_PAGES = ["account_login", "account_reset_password"]
SIGNED_IN_PAGES = [
    "mfa_index",
    "mfa_activate_totp",
    "mfa_add_webauthn",
    "mfa_list_webauthn",
    "mfa_reauthenticate",
    "account_reauthenticate",
    "account_change_password",
]


@pytest.mark.django_db
@pytest.mark.parametrize("name", ANONYMOUS_PAGES)
def test_anonymous_pages(client: Client, name: str) -> None:
    response = client.get(reverse(name))
    assert response.status_code == 200
    _assert_csp_clean(response.content.decode(), name)


@pytest.mark.django_db
def test_second_factor_page(client: Client) -> None:
    user = UserFactory.create(password=PASSWORD)
    password_login(client, user, PASSWORD)
    response = client.get(reverse("mfa_authenticate"))
    assert response.status_code == 200
    _assert_csp_clean(response.content.decode(), "mfa_authenticate")


@pytest.mark.django_db
@pytest.mark.parametrize("name", SIGNED_IN_PAGES)
def test_signed_in_pages(client: Client, name: str) -> None:
    user = UserFactory.create(password=PASSWORD)
    RecoveryCodes.activate(user)
    mfa_login(client, user, PASSWORD)
    response = client.get(reverse(name), follow=True)
    assert response.status_code == 200, name
    _assert_csp_clean(response.content.decode(), name)


@pytest.mark.django_db
def test_recovery_codes_page(client: Client) -> None:
    user = UserFactory.create(password=PASSWORD)
    RecoveryCodes.activate(user)
    with time_machine.travel(T0):
        mfa_login(client, user, PASSWORD)
    # allauth asks for an explicit re-auth here; the login code cannot be replayed,
    # so this uses the next 30-second code.
    with time_machine.travel(T0 + dt.timedelta(minutes=1)):
        mfa_reauthenticate(client, user)
        response = client.get(reverse("mfa_view_recovery_codes"))
    assert response.status_code == 200
    _assert_csp_clean(response.content.decode(), "mfa_view_recovery_codes")
