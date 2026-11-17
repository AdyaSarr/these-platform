"""Header on a narrow screen (≤ 64rem): one line, the menu button and the short
name « Thèse CIFRE »; the theme toggle and the logout are in the menu.

No script: the tools are written twice, in the header (wide screen) and at the
foot of the sidebar (.nav-tools), and layout.css shows one copy or the other
(tests/test_ui_static.py checks those rules). Pages without a sidebar keep their
tools in the header."""

from __future__ import annotations

import re

import pytest
from django.test import Client
from django.urls import reverse

from apps.accounts.roles import Role
from apps.accounts.tests.factories import UserFactory

from .helpers import signed_in

pytestmark = pytest.mark.django_db


def _part(html: str, pattern: str) -> str:
    match = re.search(pattern, html, re.S)
    assert match is not None, pattern
    return match.group(0)


def _header(html: str) -> str:
    return _part(html, r'<header class="site-header".*?</header>')


def _sidebar(html: str) -> str:
    return _part(html, r'<nav id="sidebar".*?</nav>')


def _thesis_page() -> str:
    client, thesis = signed_in(Role.PHD_STUDENT)
    response = client.get(reverse("accounts:dashboard", kwargs={"thesis_id": thesis.pk}))
    assert response.status_code == 200
    return response.content.decode()


def test_header_has_the_menu_button_and_the_short_name() -> None:
    header = _header(_thesis_page())
    assert re.search(r'<button [^>]*class="nav-toggle"', header)
    brand = _part(header, r'<a class="brand header-brand"[^>]*>.*?</a>')
    assert "<span>Thèse CIFRE</span>" in brand
    # Order on the line: menu button, short name, then the rest.
    assert header.index('class="nav-toggle"') < header.index('class="brand header-brand"')


def test_the_menu_holds_the_theme_toggle_and_the_logout() -> None:
    sidebar = _sidebar(_thesis_page())
    tools = _part(sidebar, r'<div class="nav-tools">.*?</form>\s*</div>')
    assert 'action="/ui/theme/"' in tools
    assert f'action="{reverse("logout")}"' in tools
    assert len(re.findall(r'name="theme" value="(?:auto|light|dark)"', tools)) == 3


def test_the_header_keeps_its_copy_for_wide_screens() -> None:
    tools = _part(_header(_thesis_page()), r'<div class="header-tools">.*?</div>')
    assert 'action="/ui/theme/"' in tools and f'action="{reverse("logout")}"' in tools


def test_both_copies_carry_the_same_choice_and_return_path() -> None:
    client, thesis = signed_in(Role.PHD_STUDENT)
    client.cookies["theme"] = "light"
    path = reverse("accounts:team", kwargs={"thesis_id": thesis.pk})
    html = client.get(path).content.decode()
    pressed = re.findall(r'name="theme" value="(\w+)"[^>]*aria-pressed="true"', html)
    assert pressed == ["light", "light"]
    assert html.count(f'name="next" value="{path}"') == 2


def test_page_without_sidebar_keeps_its_tools_in_the_header(client: Client) -> None:
    client.force_login(UserFactory.create())
    html = client.get(reverse("core:home")).content.decode()
    assert 'class="nav-tools"' not in html
    tools = _part(_header(html), r'<div class="header-tools">.*?</div>')
    assert 'action="/ui/theme/"' in tools
