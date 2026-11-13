"""The sign-in helpers themselves: they must not depend on the wall clock."""

import datetime as dt
import secrets

import pytest
import time_machine
from django.test import Client
from django.urls import reverse

from apps.accounts.tests.factories import UserFactory
from apps.accounts.tests.helpers import password_login, submit_totp

PASSWORD = secrets.token_urlsafe(16)
BOUNDARY = dt.datetime(2026, 12, 1, 10, 0, 30, tzinfo=dt.UTC)  # a 30-second TOTP period starts


@pytest.mark.django_db
def test_a_totp_period_ending_during_the_request_does_not_fail_the_sign_in(
    client: Client,
) -> None:
    """The clock ticks from 1 ms before a period boundary: without a frozen instant,
    the test computes the code of one period and allauth (TOTP_TOLERANCE = 0) checks
    it against the next one. This is what failed test_signed_in_pages at random."""
    user = UserFactory.create(password=PASSWORD)
    edge = BOUNDARY - dt.timedelta(milliseconds=1)
    with time_machine.travel(edge, tick=False):  # password step at the same instant
        password_login(client, user, PASSWORD)
        with time_machine.travel(edge, tick=True):  # then the clock runs across the boundary
            response = submit_totp(client, reverse("mfa_authenticate"), user.totp_secret)  # type: ignore[attr-defined]
    assert response.status_code == 302, response.content[:200]
    assert client.session.get("_auth_user_id") == str(user.pk)


@pytest.mark.django_db
def test_the_clock_ticks_again_after_submit_totp(client: Client) -> None:
    import time

    user = UserFactory.create(password=PASSWORD)
    with time_machine.travel(BOUNDARY + dt.timedelta(seconds=5), tick=True):
        password_login(client, user, PASSWORD)
        submit_totp(client, reverse("mfa_authenticate"), user.totp_secret)  # type: ignore[attr-defined]
        before = time.time()
        time.sleep(0.002)
        assert time.time() > before


@pytest.mark.django_db
def test_inside_a_frozen_travel_the_instant_is_kept(client: Client) -> None:
    """Nested in a test's own tick=False travel, submit_totp does not move the clock."""
    import time

    user = UserFactory.create(password=PASSWORD)
    with time_machine.travel(BOUNDARY + dt.timedelta(seconds=5), tick=False):
        password_login(client, user, PASSWORD)
        submit_totp(client, reverse("mfa_authenticate"), user.totp_secret)  # type: ignore[attr-defined]
        assert time.time() == (BOUNDARY + dt.timedelta(seconds=5)).timestamp()
    assert client.session.get("_auth_user_id") == str(user.pk)
