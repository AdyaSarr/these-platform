"""TOTP tolerance of one time step (MFA_TOTP_TOLERANCE = 1, RFC 6238 section 5.2).

allauth's tolerance is SYMMETRIC: at period k it accepts the codes of k-1, k
and k+1. The step before absorbs a code typed just before a 30-second boundary;
the step after is accepted too (allauth offers no one-sided setting), which a
test below states explicitly. Single use and rate limiting are unchanged.
"""

import datetime as dt
import secrets
from typing import Any

import pytest
import time_machine
from django.conf import settings
from django.test import Client
from django.urls import reverse

from apps.accounts.tests.factories import UserFactory, totp_code
from apps.accounts.tests.helpers import mfa_login, password_login

pytestmark = pytest.mark.django_db

PASSWORD = secrets.token_urlsafe(16)
# 10:00:15 UTC: the middle of period k, far from both boundaries.
T = dt.datetime(2026, 12, 1, 10, 0, 15, tzinfo=dt.UTC)
STEP = dt.timedelta(seconds=30)


def _code(user: Any, steps: int) -> str:
    """Code of period k + ``steps`` (k = the period containing T)."""
    return totp_code(user.totp_secret, at=(T + steps * STEP).timestamp())


def _second_step(client: Client, code: str) -> bool:
    client.post(reverse("mfa_authenticate"), {"code": code})
    return "_auth_user_id" in client.session


def _sign_in_with(steps: int) -> bool:
    user = UserFactory.create(password=PASSWORD)
    client = Client()
    with time_machine.travel(T, tick=False):
        password_login(client, user, PASSWORD)
        return _second_step(client, _code(user, steps))


def test_the_setting_is_one_step() -> None:
    assert settings.MFA_TOTP_TOLERANCE == 1
    assert settings.MFA_TOTP_PERIOD == 30


def test_code_of_the_current_step_is_accepted() -> None:
    assert _sign_in_with(0)


def test_code_of_the_previous_step_is_accepted() -> None:
    """Typed at 10:00:29, checked at 10:00:31: no longer refused."""
    assert _sign_in_with(-1)


def test_code_of_the_next_step_is_accepted_too() -> None:
    """allauth's window is symmetric (k-1, k, k+1): stated, not hidden."""
    assert _sign_in_with(+1)


@pytest.mark.parametrize("steps", [-2, -3, +2, +10])
def test_codes_two_steps_away_or_more_are_refused(steps: int) -> None:
    assert not _sign_in_with(steps)


# --------------------------------------------------------------------------- single use


def test_a_used_code_stays_refused_for_the_whole_window() -> None:
    """Used at period k, replayed at k+1 (where it is the PREVIOUS step, so inside
    the tolerance): the used-code marker covers the 3 steps, it is refused."""
    user = UserFactory.create(password=PASSWORD)
    code = _code(user, 0)
    first, second = Client(), Client()
    with time_machine.travel(T, tick=False):
        password_login(first, user, PASSWORD)
        assert _second_step(first, code)
    with time_machine.travel(T + STEP, tick=False):
        password_login(second, user, PASSWORD)
        assert not _second_step(second, code)


def test_a_code_used_ahead_cannot_be_replayed_once_current() -> None:
    """Used as the NEXT step at k, replayed at k+1 and k+2 (current, then previous)."""
    user = UserFactory.create(password=PASSWORD)
    code = _code(user, +1)
    with time_machine.travel(T, tick=False):
        client = Client()
        password_login(client, user, PASSWORD)
        assert _second_step(client, code)
    for steps in (1, 2):
        with time_machine.travel(T + steps * STEP, tick=False):
            replay = Client()
            password_login(replay, user, PASSWORD)
            assert not _second_step(replay, code), steps


def test_the_sign_in_code_of_the_previous_step_is_not_reusable_for_reauthentication() -> None:
    user = UserFactory.create(password=PASSWORD)
    client = Client()
    with time_machine.travel(T, tick=False):
        password_login(client, user, PASSWORD)
        code = _code(user, -1)
        assert _second_step(client, code)
        response = client.post(reverse("mfa_reauthenticate"), {"code": code})
    assert response.status_code == 200  # form shown again with an error
    assert response.context["form"].errors


# --------------------------------------------------------------------------- rate limiting


def _wrong(user: Any) -> str:
    """A code different from the three accepted ones."""
    valid = {_code(user, s) for s in (-1, 0, 1)}
    candidate = 0
    while f"{candidate:06d}" in valid:
        candidate += 1
    return f"{candidate:06d}"


def test_after_five_wrong_codes_even_the_previous_step_code_is_refused() -> None:
    user = UserFactory.create(password=PASSWORD)
    client = Client(REMOTE_ADDR="192.0.2.81")
    with time_machine.travel(T, tick=False):
        password_login(client, user, PASSWORD)
        for _ in range(5):
            assert not _second_step(client, _wrong(user))
        assert not _second_step(client, _code(user, -1))
        assert not _second_step(client, _code(user, 0))


def test_four_wrong_codes_still_leave_the_right_one_accepted() -> None:
    """The limit is still 5: the tolerance did not change the count."""
    user = UserFactory.create(password=PASSWORD)
    client = Client(REMOTE_ADDR="192.0.2.82")
    with time_machine.travel(T, tick=False):
        password_login(client, user, PASSWORD)
        for _ in range(4):
            assert not _second_step(client, _wrong(user))
        assert _second_step(client, _code(user, -1))


def test_mfa_login_helper_still_works() -> None:
    user = UserFactory.create(password=PASSWORD)
    mfa_login(Client(), user, PASSWORD)
