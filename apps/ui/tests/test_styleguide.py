"""The style guide: every component, fictitious data, in DEBUG only."""

from __future__ import annotations

import re

import pytest
from django.test import Client
from django.urls import reverse

from apps.accounts.tests.factories import UserFactory
from tests.test_csp_rendered_pages import _assert_csp_clean

pytestmark = pytest.mark.django_db

COMPONENTS = [
    "buttons", "cards", "tiles", "badges", "tables", "forms", "alerts", "progress",
    "empty", "confirm", "icons", "typography",
]  # fmt: skip


def test_absent_outside_debug(client: Client, settings) -> None:  # type: ignore[no-untyped-def]
    settings.DEBUG = False
    client.force_login(UserFactory.create())
    assert client.get(reverse("ui:styleguide")).status_code == 404


def test_requires_a_signed_in_user(client: Client, settings) -> None:  # type: ignore[no-untyped-def]
    settings.DEBUG = True
    response = client.get(reverse("ui:styleguide"))
    assert response.status_code == 302
    assert response["Location"].startswith(reverse("account_login"))


def test_shows_every_component(client: Client, settings) -> None:  # type: ignore[no-untyped-def]
    settings.DEBUG = True
    client.force_login(UserFactory.create())
    response = client.get(reverse("ui:styleguide"))
    assert response.status_code == 200
    html = response.content.decode()
    _assert_csp_clean(html, "styleguide")
    shown = re.findall(r'<section [^>]*data-component="([\w-]+)"', html)
    assert shown == COMPONENTS
    assert 'id="sidebar"' not in html  # no thesis: nothing to navigate


def test_post_is_not_allowed(client: Client, settings) -> None:  # type: ignore[no-untyped-def]
    settings.DEBUG = True
    client.force_login(UserFactory.create())
    assert client.post(reverse("ui:styleguide")).status_code == 405
