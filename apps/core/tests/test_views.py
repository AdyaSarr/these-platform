"""Acceptance: /health/ is public; the home page requires login."""

from unittest import mock

import pytest
from django.conf import settings
from django.db import OperationalError
from django.test import Client
from django.urls import reverse

from apps.accounts.tests.factories import UserFactory


@pytest.mark.django_db
def test_health_is_public_and_minimal(client: Client) -> None:
    response = client.get("/health/")
    assert response.status_code == 200
    assert response["Content-Type"] == "application/json"
    assert response.json() == {"status": "ok"}
    assert "no-store" in response["Cache-Control"]


def test_health_returns_503_without_leaking_details(client: Client) -> None:
    with mock.patch("apps.core.views.connection") as conn:
        conn.cursor.side_effect = OperationalError("password authentication failed for user x")
        response = client.get("/health/")
    assert response.status_code == 503
    assert response.json() == {"status": "unavailable"}
    assert b"password" not in response.content


def test_health_rejects_post(client: Client) -> None:
    assert client.post("/health/").status_code == 405


def test_home_requires_login(client: Client) -> None:
    response = client.get(reverse("core:home"))
    assert response.status_code == 302
    assert response["Location"] == f"{settings.LOGIN_URL}?next=/"


@pytest.mark.django_db
def test_home_for_authenticated_user(client: Client) -> None:
    client.force_login(UserFactory.create())
    response = client.get(reverse("core:home"))
    assert response.status_code == 200
    assert b'lang="fr"' in response.content


@pytest.mark.django_db
def test_logout_is_post_only(client: Client) -> None:
    client.force_login(UserFactory.create())
    assert client.get(reverse("logout")).status_code == 405
    assert client.post(reverse("logout")).status_code == 302
