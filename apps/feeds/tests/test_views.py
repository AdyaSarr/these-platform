"""The views of the feed (M12): the block on « Mes notifications », create (recent
second factor), regenerate (recent second factor), revoke (POST with CSRF, no
second factor). One allowed and one refused test per role."""

from __future__ import annotations

import re
from types import SimpleNamespace
from typing import Any

import pytest
from django.core.cache import cache
from django.urls import reverse

from apps.feeds.models import CalendarFeed
from apps.feeds.tests import helpers as h

pytestmark = pytest.mark.django_db(transaction=True)
ALL = (*h.ALLOWED_ROLES, "csi", "guest")
TOKEN_IN_PAGE = re.compile(r"/ical/[A-Za-z0-9_-]{43}\.ics")


@pytest.fixture
def team(time_machine: Any) -> SimpleNamespace:
    cache.clear()
    time_machine.move_to(h.NOW, tick=True)
    return h.make_team()


def _prefs(team: SimpleNamespace) -> str:
    return reverse("activity:preferences", kwargs={"thesis_id": team.thesis.pk})


def _mfa_browser(user: Any) -> Any:
    from django.test import Client

    from apps.accounts.tests.helpers import mfa_login

    client = Client(enforce_csrf_checks=True)
    mfa_login(client, user, h.PASSWORD)
    return client


@pytest.mark.parametrize("role", h.ALLOWED_ROLES)
def test_the_block_is_shown_to_allowed_roles(team: SimpleNamespace, role: str) -> None:
    html = h.client_for(getattr(team, role)).get(_prefs(team)).content.decode()
    assert "Abonnement agenda" in html and h.page(team, "create") in html


@pytest.mark.parametrize("role", ["csi"])
def test_no_block_for_the_csi_member(team: SimpleNamespace, role: str) -> None:
    html = h.client_for(getattr(team, role)).get(_prefs(team)).content.decode()
    assert "Abonnement agenda" not in html


@pytest.mark.parametrize("role", h.ALLOWED_ROLES)
def test_create_shows_the_url_once(team: SimpleNamespace, role: str) -> None:
    user = getattr(team, role)
    browser = _mfa_browser(user)
    response = h.browser_post(browser, _prefs(team), h.page(team, "create"), {})
    assert response.status_code == 200
    assert (
        response["Cache-Control"].startswith("private") and "no-store" in response["Cache-Control"]
    )
    html = response.content.decode()
    assert len(TOKEN_IN_PAGE.findall(html)) >= 1 and "webcal://" in html
    assert "Ajouter un calendrier avec abonnement" in html and "iCloud" in html
    again = browser.get(_prefs(team)).content.decode()
    assert not TOKEN_IN_PAGE.search(again)  # never shown again


def test_create_without_a_recent_second_factor_asks_for_it(team: SimpleNamespace) -> None:
    browser = h.browser_for(team.director)  # signed in, no recent factor
    response = h.browser_post(browser, _prefs(team), h.page(team, "create"), {})
    assert response.status_code == 302 and reverse("mfa_reauthenticate") in response["Location"]
    assert not CalendarFeed.objects.exists()


@pytest.mark.parametrize("role", ["csi", "guest", "outsider"])
def test_create_is_refused_to_excluded_roles(team: SimpleNamespace, role: str) -> None:
    browser = _mfa_browser(getattr(team, role))
    page = h.browser_post(browser, reverse("core:home"), h.page(team, "create"), {})
    assert page.status_code == 403
    assert not CalendarFeed.objects.exists()


def test_regenerate_needs_a_recent_second_factor(team: SimpleNamespace) -> None:
    h.new_feed(team.director, team.thesis)
    browser = h.browser_for(team.director)
    response = h.browser_post(browser, _prefs(team), h.page(team, "regenerate"), {})
    assert response.status_code == 302 and reverse("mfa_reauthenticate") in response["Location"]


@pytest.mark.parametrize("role", h.ALLOWED_ROLES)
def test_revoke_without_a_second_factor(team: SimpleNamespace, role: str) -> None:
    user = getattr(team, role)
    _feed, token = h.new_feed(user, team.thesis)
    browser = h.browser_for(user)  # no recent factor: not needed to revoke
    response = h.browser_post(browser, _prefs(team), h.page(team, "revoke"), {})
    assert response.status_code == 302
    assert h.fetch(token).status_code == 404


def test_revoke_without_csrf_is_refused(team: SimpleNamespace) -> None:
    _feed, token = h.new_feed(team.director, team.thesis)
    browser = h.browser_for(team.director)
    assert browser.post(h.page(team, "revoke"), HTTP_ORIGIN=h.SITE_ORIGIN).status_code == 403
    assert h.fetch(token).status_code == 200


@pytest.mark.parametrize("role", ALL)
def test_another_thesis_is_refused(team: SimpleNamespace, role: str) -> None:
    other = h.make_team()
    browser = h.browser_for(getattr(team, role))
    for name in ("create", "regenerate", "revoke"):
        response = browser.post(h.page(other, name), HTTP_ORIGIN=h.SITE_ORIGIN)
        assert response.status_code == 403, name


def test_the_block_shows_the_dates_but_never_the_url(team: SimpleNamespace) -> None:
    h.new_feed(team.director, team.thesis)
    h.fetch  # noqa: B018
    html = h.client_for(team.director).get(_prefs(team)).content.decode()
    assert "Expire le" in html and "Dernière utilisation" in html
    assert not TOKEN_IN_PAGE.search(html)
