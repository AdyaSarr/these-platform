"""Sidebar and header: only the modules the role may open, only after the guard of
permissions.py authorised the page (request.thesis), never on a refused page."""

from __future__ import annotations

import re
import uuid

import pytest
from django.test import Client
from django.urls import resolve, reverse

from apps.accounts.permissions import ROLE_ACTIONS
from apps.accounts.roles import Role
from apps.accounts.tests.factories import ThesisFactory, UserFactory

from .helpers import member, nav_keys, signed_in

pytestmark = pytest.mark.django_db

EXPECTED_ORDER = [
    "home", "activity", "admin_file", "objectives", "meetings", "conferences", "travel",
    "portfolio", "documents", "team", "csi",
]  # fmt: skip


def _nav():  # type: ignore[no-untyped-def]
    from apps.ui.navigation import NAV

    return NAV


def _required_action(url: str) -> object:
    callback = resolve(url).func
    view_class = getattr(callback, "view_class", None)
    if view_class is not None:
        return view_class.required_action
    return callback.thesis_permission_action  # type: ignore[attr-defined]


def test_items_in_order() -> None:
    assert [item.key for item in _nav()] == EXPECTED_ORDER


def test_whoever_sees_an_item_may_open_its_page() -> None:
    for item in _nav():
        url = reverse(item.url_name, kwargs={"thesis_id": uuid.uuid4()})
        guard = _required_action(url)
        for role, actions in ROLE_ACTIONS.items():
            if item.action in actions:
                assert guard in actions, (item.key, role)


def test_an_item_shows_no_more_than_the_dashboard_card_of_its_module() -> None:
    """Same action as the module's card: the sidebar never adds a module the
    dashboard hides (the document list itself only requires VIEW_THESIS)."""
    from apps.accounts.views import DASHBOARD_BLOCKS

    cards = {block.key: block.action for block in DASHBOARD_BLOCKS}
    for item in _nav():
        if item.key in cards:
            assert item.action == cards[item.key], item.key


def _page_of(role: Role) -> str:
    # A CSI member is sent from the dashboard to the CSI file (accounts.views).
    return "accounts:csi_file" if role is Role.CSI_MEMBER else "accounts:dashboard"


@pytest.mark.parametrize("role", [r for r in Role if r is not Role.GUEST])
def test_sidebar_shows_the_allowed_modules_only(role: Role) -> None:
    client, thesis = signed_in(role)
    response = client.get(reverse(_page_of(role), kwargs={"thesis_id": thesis.pk}))
    assert response.status_code == 200
    expected = [item.key for item in _nav() if item.action in ROLE_ACTIONS[role]]
    assert nav_keys(response.content.decode()) == expected


def test_guest_sees_no_sidebar_and_no_title() -> None:
    client, thesis = signed_in(Role.GUEST)
    response = client.get(reverse("accounts:dashboard", kwargs={"thesis_id": thesis.pk}))
    assert response.status_code == 403
    html = response.content.decode()
    assert nav_keys(html) == [] and 'id="sidebar"' not in html
    assert thesis.title not in html


def test_refused_page_shows_neither_sidebar_nor_the_other_thesis_title() -> None:
    client, _ = signed_in(Role.DIRECTOR)
    other = ThesisFactory.create(title="Sujet fictif d'une autre thèse")
    member(other, Role.PHD_STUDENT)
    response = client.get(reverse("accounts:dashboard", kwargs={"thesis_id": other.pk}))
    assert response.status_code == 403
    html = response.content.decode()
    assert 'id="sidebar"' not in html
    assert other.title not in html


def test_pages_outside_a_thesis_have_no_sidebar(client: Client) -> None:
    assert 'id="sidebar"' not in client.get(reverse("account_login")).content.decode()
    user = UserFactory.create()
    client.force_login(user)
    response = client.get(reverse("core:home"))
    assert response.status_code == 200
    assert 'id="sidebar"' not in response.content.decode()


def test_header_shows_the_thesis_title_to_a_member() -> None:
    client, thesis = signed_in(Role.CO_SUPERVISOR)
    html = client.get(reverse("accounts:team", kwargs={"thesis_id": thesis.pk})).content.decode()
    header = re.search(r'<header class="site-header".*?</header>', html, re.S)
    assert header is not None and thesis.title in header.group(0)


@pytest.mark.parametrize(
    ("url_name", "current"),
    [
        ("accounts:dashboard", "home"),
        ("accounts:team", "team"),
        ("objectives:dashboard", "objectives"),
    ],
)
def test_the_current_item_is_marked(url_name: str, current: str) -> None:
    client, thesis = signed_in(Role.PHD_STUDENT)
    html = client.get(reverse(url_name, kwargs={"thesis_id": thesis.pk})).content.decode()
    # In the sidebar: the module's tabs and segmented controls mark their own.
    sidebar = re.search(r'<nav id="sidebar".*?</nav>', html, re.S)
    assert sidebar is not None
    marked = re.findall(r'<a [^>]*aria-current="page"[^>]*>', sidebar.group(0))
    assert len(marked) == 1, marked
    assert f'data-nav="{current}"' in marked[0]


def test_mobile_menu_is_a_popover_with_a_native_button() -> None:
    client, thesis = signed_in(Role.PHD_STUDENT)
    html = client.get(
        reverse("accounts:dashboard", kwargs={"thesis_id": thesis.pk})
    ).content.decode()
    assert re.search(r'<nav id="sidebar" class="sidebar" popover aria-label="[^"]+"', html)
    assert re.search(r'<button [^>]*class="nav-toggle"[^>]*popovertarget="sidebar"', html)
    assert re.search(r'popovertarget="sidebar" popovertargetaction="hide"', html)


def test_skip_link_targets_the_main_content() -> None:
    client, thesis = signed_in(Role.PHD_STUDENT)
    html = client.get(
        reverse("accounts:dashboard", kwargs={"thesis_id": thesis.pk})
    ).content.decode()
    assert re.search(r'<a class="skip-link" href="#main">', html)
    assert '<main id="main"' in html
    # The skip link comes first in the body, before the sidebar.
    assert html.index('class="skip-link"') < html.index('id="sidebar"')


def test_request_thesis_is_set_only_by_a_successful_guard() -> None:
    from apps.ui.context_processors import ui

    client, thesis = signed_in(Role.PHD_STUDENT)
    ok = client.get(reverse("accounts:team", kwargs={"thesis_id": thesis.pk}))
    assert ok.wsgi_request.thesis == thesis  # type: ignore[attr-defined]
    refused = client.get(reverse("accounts:team", kwargs={"thesis_id": uuid.uuid4()}))
    assert refused.status_code in (403, 404)
    assert not hasattr(refused.wsgi_request, "thesis")
    assert list(ui(refused.wsgi_request)["ui_nav"]) == []
