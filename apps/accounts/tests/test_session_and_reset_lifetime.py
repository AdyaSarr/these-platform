"""SR-08: a password-reset link lasts one hour; SR-18: the session cookie ends with
the browser, while the server still ends the session after 8 hours
(docs/security/revue-2026-11.md)."""

from __future__ import annotations

import datetime as dt
import re
import secrets
from typing import Any

import pytest
from django.conf import settings
from django.contrib.sessions.models import Session
from django.core import mail
from django.test import Client
from django.utils import timezone

from apps.accounts.tests.factories import UserFactory
from apps.accounts.tests.helpers import mfa_login

pytestmark = pytest.mark.django_db

PASSWORD = secrets.token_urlsafe(16)
T0 = dt.datetime(2026, 12, 1, 10, 0, tzinfo=dt.UTC)


# --------------------------------------------------------------------------- SR-08


def test_the_generator_allauth_uses_reads_the_one_hour_timeout(time_machine: Any) -> None:
    """The token of accounts.tasks comes from allauth's generator, which keeps
    Django's check_token (and so PASSWORD_RESET_TIMEOUT)."""
    from allauth.account import app_settings as account_settings

    user = UserFactory.create(mfa=False)
    generator = account_settings.PASSWORD_RESET_TOKEN_GENERATOR()
    time_machine.move_to(T0, tick=False)
    token = generator.make_token(user)
    time_machine.move_to(T0 + dt.timedelta(seconds=3599), tick=False)
    assert generator.check_token(user, token)
    time_machine.move_to(T0 + dt.timedelta(seconds=3601), tick=False)
    assert not generator.check_token(user, token)


def _reset_link_path(user: Any) -> str:
    from apps.accounts.tasks import send_password_reset_email

    mail.outbox.clear()
    send_password_reset_email(str(user.pk))
    link = re.search(r"https?://\S+", str(mail.outbox[0].body)).group(0)  # type: ignore[union-attr]
    return "/" + link.split("/", 3)[3]


@pytest.mark.parametrize(("after", "valid"), [(3599, True), (3601, False)])
def test_the_link_of_the_e_mail_expires_after_one_hour(
    time_machine: Any, after: int, valid: bool
) -> None:
    user = UserFactory.create(mfa=False)
    time_machine.move_to(T0, tick=False)
    path = _reset_link_path(user)
    time_machine.move_to(T0 + dt.timedelta(seconds=after), tick=False)
    page = Client().get(path, follow=True)
    assert page.status_code == 200
    assert bool(page.context.get("token_fail")) is not valid


# --------------------------------------------------------------------------- SR-18


def test_the_session_cookie_ends_with_the_browser(time_machine: Any) -> None:
    user = UserFactory.create(password=PASSWORD)  # TOTP enrolled
    client = Client()
    time_machine.move_to(T0, tick=True)
    mfa_login(client, user, PASSWORD)
    cookie = client.cookies[settings.SESSION_COOKIE_NAME]
    assert cookie.value
    assert cookie["max-age"] == "" and cookie["expires"] == "", cookie.output()


def test_the_server_still_ends_the_session_after_eight_hours(time_machine: Any) -> None:
    user = UserFactory.create(password=PASSWORD)
    client = Client()
    time_machine.move_to(T0, tick=True)
    mfa_login(client, user, PASSWORD)
    session = Session.objects.get(session_key=client.cookies[settings.SESSION_COOKIE_NAME].value)
    left = session.expire_date - timezone.now()
    assert dt.timedelta(hours=8) - dt.timedelta(minutes=1) < left <= dt.timedelta(hours=8)
