"""apps/accounts/reauth.py: the re-authentication guard other modules reuse."""

import datetime as dt
import inspect
import secrets
from typing import Any

import pytest
import time_machine
from django.http import HttpRequest, HttpResponse
from django.test import Client
from django.urls import path, reverse
from django.views import View

from apps.accounts.reauth import (
    REAUTH_MAX_AGE,
    RecentAuthRequiredMixin,
    recent_auth_required,
)
from apps.accounts.tests.factories import UserFactory
from apps.accounts.tests.helpers import mfa_login, mfa_reauthenticate, password_reauthenticate
from config.urls import urlpatterns as project_urlpatterns

pytestmark = [pytest.mark.django_db, pytest.mark.urls(__name__)]

PASSWORD = secrets.token_urlsafe(16)
T0 = dt.datetime(2026, 12, 1, 10, 0, tzinfo=dt.UTC)


@recent_auth_required
def strict_view(request: HttpRequest) -> HttpResponse:
    return HttpResponse("strict")


@recent_auth_required(mfa=False)
def lenient_view(request: HttpRequest) -> HttpResponse:
    return HttpResponse("lenient")


class StrictMixinView(RecentAuthRequiredMixin, View):
    def get(self, request: HttpRequest) -> HttpResponse:
        return HttpResponse("mixin")

    def post(self, request: HttpRequest) -> HttpResponse:
        return HttpResponse("mixin-post")


# These test views, plus the project's routes (login, allauth re-auth pages).
urlpatterns = [
    path("strict/", strict_view),
    path("lenient/", lenient_view),
    path("mixin/", StrictMixinView.as_view()),
    *project_urlpatterns,
]


@pytest.fixture
def user() -> Any:
    return UserFactory.create(password=PASSWORD)


def test_mfa_is_required_by_default() -> None:
    """A module gets password-only re-auth only by writing mfa=False."""
    assert inspect.signature(recent_auth_required).parameters["mfa"].default is True
    assert RecentAuthRequiredMixin.reauth_mfa is True
    assert strict_view.recent_auth_mfa is True  # type: ignore[attr-defined]
    assert lenient_view.recent_auth_mfa is False  # type: ignore[attr-defined]


def test_max_age_is_ten_minutes() -> None:
    assert REAUTH_MAX_AGE == 600


def test_anonymous_is_sent_to_login(client: Client) -> None:
    response = client.get("/strict/")
    assert response["Location"].startswith(reverse("account_login"))


@pytest.mark.parametrize("url", ["/strict/", "/mixin/"])
def test_fresh_mfa_sign_in_counts(client: Client, user: Any, url: str) -> None:
    with time_machine.travel(T0):
        mfa_login(client, user, PASSWORD)
    with time_machine.travel(T0 + dt.timedelta(seconds=REAUTH_MAX_AGE - 5)):
        assert client.get(url).status_code == 200


@pytest.mark.parametrize("url", ["/strict/", "/mixin/"])
def test_older_than_ten_minutes_asks_for_the_factor_again(
    client: Client, user: Any, url: str
) -> None:
    with time_machine.travel(T0):
        mfa_login(client, user, PASSWORD)
    with time_machine.travel(T0 + dt.timedelta(seconds=REAUTH_MAX_AGE + 1)):
        response = client.get(url)
    assert response.status_code == 302
    assert response["Location"] == f"{reverse('mfa_reauthenticate')}?next={url.replace('/', '%2F')}"


def test_password_reauth_does_not_satisfy_the_default(client: Client, user: Any) -> None:
    with time_machine.travel(T0):
        mfa_login(client, user, PASSWORD)
    with time_machine.travel(T0 + dt.timedelta(minutes=30)):
        password_reauthenticate(client, PASSWORD)
        assert client.get("/strict/").status_code == 302
        assert client.get("/lenient/").status_code == 200


def test_mfa_reauth_satisfies_both(client: Client, user: Any) -> None:
    with time_machine.travel(T0):
        mfa_login(client, user, PASSWORD)
    with time_machine.travel(T0 + dt.timedelta(minutes=30)):
        mfa_reauthenticate(client, user)
        assert client.get("/strict/").status_code == 200
        assert client.get("/lenient/").status_code == 200


def test_lenient_view_sends_to_password_reauth(client: Client, user: Any) -> None:
    with time_machine.travel(T0):
        mfa_login(client, user, PASSWORD)
    with time_machine.travel(T0 + dt.timedelta(minutes=30)):
        response = client.get("/lenient/")
    assert response["Location"].startswith(reverse("account_reauthenticate"))


def test_force_login_session_has_no_recent_authentication(client: Client, user: Any) -> None:
    client.force_login(user)
    assert client.get("/strict/").status_code == 302


def test_htmx_gets_hx_redirect(client: Client, user: Any) -> None:
    client.force_login(user)
    response = client.post(
        "/mixin/", headers={"HX-Request": "true", "HX-Current-URL": "http://testserver/t/x/team/"}
    )
    assert response.status_code == 200
    assert response["HX-Redirect"] == f"{reverse('mfa_reauthenticate')}?next=%2Ft%2Fx%2Fteam%2F"
    assert response.content == b""


def test_post_returns_to_a_same_site_page_only(client: Client, user: Any) -> None:
    client.force_login(user)
    ok = client.post("/mixin/", headers={"Referer": "http://testserver/t/x/team/"})
    assert ok["Location"].endswith("?next=%2Ft%2Fx%2Fteam%2F")
    evil = client.post("/mixin/", headers={"Referer": "https://evil.example/phish/"})
    assert evil["Location"].endswith("?next=%2F")
