"""Every page each role can reach renders without error, under the strict CSP, with
the sidebar of that role and nothing more.

The thesis is the fictitious demonstration thesis (apps/demo/scenario.py: every
module filled through its services). For each role the test follows, from the
dashboard, every link and htmx GET to a page of that thesis, as a reader would.
"""

from __future__ import annotations

import html as html_module
import re
from collections import deque

import pytest
from django.test import Client

from apps.accounts.models import Membership, Thesis
from apps.accounts.permissions import ROLE_ACTIONS
from apps.accounts.roles import Role
from apps.accounts.tests.factories import enroll_totp
from apps.demo import scenario
from apps.ui.tests.helpers import nav_keys
from tests.test_csp_rendered_pages import _assert_csp_clean

pytestmark = [pytest.mark.django_db(transaction=True), pytest.mark.without_vault_keys]

MAX_PAGES = 400
LINK = re.compile(r"""(?:href|hx-get)="([^"#]+)""")
ROLES = [
    Role.PHD_STUDENT,
    Role.DIRECTOR,
    Role.CO_SUPERVISOR,
    Role.INDUSTRIAL_TUTOR,
    Role.CSI_MEMBER,
]


def _links(html: str, prefix: str) -> set[str]:
    return {
        url
        for url in (html_module.unescape(u) for u in LINK.findall(html))
        if url.startswith(prefix)
    }


def _crawl(client: Client, start: str, prefix: str, role: Role) -> dict[str, int]:
    from apps.ui.navigation import NAV

    expected_nav = [item.key for item in NAV if item.action in ROLE_ACTIONS[role]]
    seen: dict[str, int] = {}
    queue = deque([start])
    while queue and len(seen) < MAX_PAGES:
        url = queue.popleft()
        if url in seen:
            continue
        response = client.get(url)
        seen[url] = response.status_code
        assert response.status_code < 500, (role, url)
        if response.status_code != 200 or "text/html" not in response.get("Content-Type", ""):
            continue
        page = response.content.decode()
        _assert_csp_clean(page, f"{role} {url}")
        if "<html" in page:
            assert nav_keys(page) == expected_nav, (role, url)
            assert len(re.findall(r'aria-current="page"', page)) <= 1, (role, url)
        queue.extend(_links(page, prefix) - seen.keys())
    assert len(seen) < MAX_PAGES, "the crawl did not finish"
    return seen


def test_every_reachable_page_renders_for_every_role(settings) -> None:  # type: ignore[no-untyped-def]
    settings.DEBUG = True  # demo_seed refuses to run otherwise (apps/demo/guard.py)
    scenario.seed()
    settings.DEBUG = False
    thesis = Thesis.objects.get(title=scenario.DEMO_TITLE)
    prefix = f"/t/{thesis.pk}/"
    reached = {}
    for role in ROLES:
        user = Membership.objects.active().get(thesis=thesis, role=role).user
        enroll_totp(user)  # demo accounts have no factor yet: the middleware would redirect
        client = Client()
        client.force_login(user)
        reached[role] = _crawl(client, prefix, prefix, role)
    # The crawl really went through the modules.
    assert len([u for u, s in reached[Role.PHD_STUDENT].items() if s == 200]) > 30
    for role in ROLES:
        assert any(s == 200 for s in reached[role].values()), role
