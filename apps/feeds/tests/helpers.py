"""Shared fixtures of the iCal feed tests (M12). Fictitious data only."""

from __future__ import annotations

from typing import Any

from django.test import Client
from django.urls import reverse

from apps.activity.tests.helpers import (  # noqa: F401  (re-exported for the tests)
    NOW,
    PASSWORD,
    ROLE_ATTRS,
    SITE_ORIGIN,
    D,
    browser_for,
    browser_post,
    client_for,
    document,
    item,
    make_team,
    meeting,
    mission,
    objective,
    pdf,
    submission,
)

ALLOWED_ROLES = ("student", "director", "co_supervisor", "tutor", "st_co_supervisor")


def new_feed(user: Any, thesis: Any) -> tuple[Any, str]:
    """A live feed of ``user`` on ``thesis`` and its token (shown once)."""
    from apps.feeds import services

    return services.create(user=user, thesis=thesis, recent_mfa=True)


def feed_url(token: str) -> str:
    return reverse("feeds:ical", kwargs={"token": token})


def fetch(token: str, client: Client | None = None) -> Any:
    return (client or Client()).get(feed_url(token))


def page(team: Any, name: str) -> str:
    return reverse(f"feeds:{name}", kwargs={"thesis_id": team.thesis.pk})
