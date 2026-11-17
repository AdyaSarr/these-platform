"""The HTTP answer of the feed (M12): unknown, revoked, expired, finished thesis,
ended membership, excluded role → the same 404, body and headers, after one
query; no cookie read or set; private, no-store, noindex, no-referrer; GET and
HEAD only; 60/h per token, 300/h per address, counted before the lookup."""

from __future__ import annotations

import datetime as dt
from types import SimpleNamespace
from typing import Any

import pytest
from django.core.cache import cache
from django.test import Client

from apps.accounts.models import Membership, Thesis
from apps.feeds import services
from apps.feeds.models import CalendarFeed
from apps.feeds.tests import helpers as h

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture(autouse=True)
def _clear_limits() -> None:
    cache.clear()


@pytest.fixture
def team(time_machine: Any) -> SimpleNamespace:
    time_machine.move_to(h.NOW, tick=True)
    return h.make_team()


def _refusals(team: SimpleNamespace) -> list[str]:
    """Five tokens that must look alike: unknown, revoked, expired, of a finished
    thesis, of an ended membership."""
    tokens = ["a" * 43]
    _f, revoked = h.new_feed(team.director, team.thesis)
    services.revoke(user=team.director, thesis=team.thesis)
    tokens.append(revoked)
    feed, expired = h.new_feed(team.tutor, team.thesis)
    CalendarFeed.objects.filter(pk=feed.pk).update(expires_on=h.NOW.date() - dt.timedelta(days=1))
    tokens.append(expired)
    other = h.make_team()
    _f, finished = h.new_feed(other.director, other.thesis)
    Thesis.objects.filter(pk=other.thesis.pk).update(status=Thesis.Status.CLOSED)
    tokens.append(finished)
    _f, ended = h.new_feed(team.co_supervisor, team.thesis)
    Membership.objects.filter(user=team.co_supervisor).update(active_until=h.NOW)
    tokens.append(ended)
    return tokens


def test_every_refusal_looks_the_same(team: SimpleNamespace,
                                      django_assert_num_queries: Any) -> None:  # fmt: skip
    answers = []
    for token in _refusals(team):
        with django_assert_num_queries(1):
            response = h.fetch(token)
        headers = {k: v for k, v in response.items() if k.lower() != "date"}
        answers.append((response.status_code, response.content, tuple(sorted(headers.items()))))
    assert answers[0][0] == 404
    assert all(a == answers[0] for a in answers), answers


def test_no_cookie_read_or_set(team: SimpleNamespace) -> None:
    _feed, token = h.new_feed(team.director, team.thesis)
    browser = Client()
    browser.force_login(team.director)  # a session cookie the feed must ignore
    response = h.fetch(token, browser)
    assert response.status_code == 200
    assert "Set-Cookie" not in response and not response.cookies
    session = response.wsgi_request.session
    assert not hasattr(session, "_session_cache")  # never loaded


def test_the_headers(team: SimpleNamespace) -> None:
    _feed, token = h.new_feed(team.director, team.thesis)
    for response in (h.fetch(token), h.fetch("b" * 43)):
        assert response["Cache-Control"] == "private, no-store"
        assert response["X-Robots-Tag"] == "noindex, nofollow"
        assert response["Referrer-Policy"] == "no-referrer"
    assert h.fetch(token)["Content-Type"] == "text/calendar; charset=utf-8"


def test_get_and_head_only(team: SimpleNamespace) -> None:
    _feed, token = h.new_feed(team.director, team.thesis)
    assert Client().head(h.feed_url(token)).status_code == 200
    for method in ("post", "put", "delete", "patch"):
        assert getattr(Client(), method)(h.feed_url(token)).status_code == 405


def test_60_per_hour_per_token_the_same_for_a_valid_one_or_not(team: SimpleNamespace) -> None:
    _feed, token = h.new_feed(team.director, team.thesis)
    for candidate in (token, "c" * 43):
        cache.clear()
        statuses = [h.fetch(candidate).status_code for _ in range(61)]
        assert statuses[-1] == 429 and 429 not in statuses[:60]
    cache.clear()
    valid = [h.fetch(token) for _ in range(61)][-1]
    cache.clear()
    invalid = [h.fetch("c" * 43) for _ in range(61)][-1]
    assert (valid.status_code, valid.content) == (invalid.status_code, invalid.content)


def test_300_per_hour_per_address(team: SimpleNamespace) -> None:
    statuses = [h.fetch(f"{n:043d}").status_code for n in range(301)]
    assert statuses[-1] == 429 and 429 not in statuses[:300]
