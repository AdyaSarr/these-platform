"""Light / dark toggle: a POST form (no script), a cookie from a closed list,
HttpOnly, SameSite=Lax, Secure where the session cookie is; never an open redirect."""

from __future__ import annotations

import re

import pytest
from django.test import Client
from django.urls import reverse

from apps.accounts.roles import Role

from .helpers import signed_in

pytestmark = pytest.mark.django_db

ORIGIN = "http://testserver"


def _post(client: Client, **data: str):  # type: ignore[no-untyped-def]
    return client.post(reverse("ui:theme"), data, HTTP_ORIGIN=ORIGIN)


@pytest.mark.parametrize("choice", ["light", "dark"])
def test_choice_sets_the_cookie_and_returns_to_the_page(client: Client, choice: str) -> None:
    response = _post(client, theme=choice, next="/accounts/login/")
    assert response.status_code == 302
    assert response["Location"] == "/accounts/login/"
    cookie = response.cookies["theme"]
    assert cookie.value == choice
    assert cookie["httponly"] is True
    assert cookie["samesite"] == "Lax"
    assert cookie["path"] == "/"
    assert int(cookie["max-age"]) == 365 * 24 * 3600
    assert not cookie["secure"]  # test settings: SESSION_COOKIE_SECURE is False


def test_cookie_is_secure_where_the_session_cookie_is(client: Client, settings) -> None:  # type: ignore[no-untyped-def]
    settings.SESSION_COOKIE_SECURE = True
    response = _post(client, theme="dark", next="/")
    assert response.cookies["theme"]["secure"] is True


def test_auto_removes_the_cookie(client: Client) -> None:
    client.cookies["theme"] = "dark"
    response = _post(client, theme="auto", next="/")
    assert response.status_code == 302
    assert response.cookies["theme"].value == ""
    assert response.cookies["theme"]["max-age"] == 0


@pytest.mark.parametrize("value", ["", "blue", "dark ", "DARK", '"><script>'])
def test_a_value_outside_the_list_is_refused(client: Client, value: str) -> None:
    response = _post(client, theme=value, next="/")
    assert response.status_code == 400
    assert "theme" not in response.cookies


@pytest.mark.parametrize(
    "target",
    ["https://evil.example/", "//evil.example/", "/\\evil.example", "javascript:alert(1)", ""],
)
def test_never_an_open_redirect(client: Client, target: str) -> None:
    response = _post(client, theme="dark", next=target)
    assert response.status_code == 302
    assert response["Location"] == "/"


def test_get_is_not_allowed(client: Client) -> None:
    assert client.get(reverse("ui:theme")).status_code == 405


def test_like_a_browser_with_the_csrf_token_of_the_page() -> None:
    browser = Client(enforce_csrf_checks=True)
    page = browser.get(reverse("account_login"))
    token = re.search(
        r'action="/ui/theme/".*?name="csrfmiddlewaretoken" value="([^"]+)"',
        page.content.decode(),
        re.S,
    )
    assert token is not None
    response = browser.post(
        reverse("ui:theme"),
        {"csrfmiddlewaretoken": token.group(1), "theme": "dark", "next": "/accounts/login/"},
        HTTP_ORIGIN=ORIGIN,
    )
    assert response.status_code == 302
    assert (
        Client(enforce_csrf_checks=True)
        .post(reverse("ui:theme"), {"theme": "dark"}, HTTP_ORIGIN=ORIGIN)
        .status_code
        == 403
    )


def _html_tag(client: Client, path: str) -> str:
    match = re.search(r"<html[^>]*>", client.get(path).content.decode())
    assert match is not None
    return match.group(0)


@pytest.mark.parametrize("choice", ["light", "dark"])
def test_the_page_carries_the_chosen_theme(client: Client, choice: str) -> None:
    client.cookies["theme"] = choice
    assert f'data-theme="{choice}"' in _html_tag(client, reverse("account_login"))


@pytest.mark.parametrize("value", ["auto", "", "blue", '"><script>alert(1)</script>'])
def test_no_theme_attribute_outside_the_list(client: Client, value: str) -> None:
    client.cookies["theme"] = value
    tag = _html_tag(client, reverse("account_login"))
    assert "data-theme" not in tag
    assert "<script>alert" not in client.get(reverse("account_login")).content.decode()


def test_the_current_choice_is_pressed(client: Client) -> None:
    client.cookies["theme"] = "dark"
    html = client.get(reverse("account_login")).content.decode()
    pressed = re.findall(r'<button [^>]*name="theme" value="(\w+)"[^>]*aria-pressed="true"', html)
    assert pressed == ["dark"]
    assert len(re.findall(r'name="theme" value="(?:auto|light|dark)"', html)) == 3


def test_next_is_the_path_without_the_query_string() -> None:
    client, thesis = signed_in(Role.PHD_STUDENT)
    path = reverse("accounts:team", kwargs={"thesis_id": thesis.pk})
    html = client.get(path + "?x=1").content.decode()
    assert f'name="next" value="{path}"' in html
