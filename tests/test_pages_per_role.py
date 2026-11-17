"""Every page each role can reach renders without error, under the strict CSP, with
the sidebar of that role and nothing more.

Every full page also has the common structure (apps/ui/tests/test_page_structure.py):
one <h1>, in the page header; at most one current item in the sidebar and in the
module's tabs; no link back to the thesis dashboard in the content
(the sidebar does it); no bar of links separated by dots.

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


LINK_BAR = re.compile(r"</a>\s*(?:·|&middot;|&#183;)\s*<a\b")


def _assert_structure(page: str, prefix: str, where: object) -> None:
    main = re.search(r"<main\b.*?</main>", page, re.S)
    assert main is not None, where
    content = main.group(0)
    assert len(re.findall(r"<h1\b", content)) == 1, where
    header = re.search(r'<header class="page-header">.*?</header>', content, re.S)
    assert header is not None and "<h1" in header.group(0), where
    assert f'href="{prefix}"' not in content, where  # the dashboard: the sidebar links it
    assert not LINK_BAR.search(content), (where, LINK_BAR.search(content))


PRIMARY = re.compile(r'<button\b[^>]*class="[^"]*\bbtn-primary\b|<a\b[^>]*class="btn btn-primary"')


def _crowded_blocks(page: str) -> list[str]:
    """The forms and sections of a page with more than one primary button."""
    blocks = re.findall(r"<form\b.*?</form>", page, re.S)
    blocks += re.findall(r"<section\b.*?</section>", page, re.S)
    blocks += re.findall(r'<header class="page-header">.*?</header>', page, re.S)
    return [b[:70] for b in blocks if len(PRIMARY.findall(b)) > 1]


def _links(html: str, prefix: str) -> set[str]:
    return {
        url
        for url in (html_module.unescape(u) for u in LINK.findall(html))
        if url.startswith(prefix)
    }


def _crawl(
    client: Client, start: str, prefix: str, role: Role, crowded: list[object] | None = None
) -> dict[str, int]:
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
        if response.status_code in (301, 302) and response["Location"].startswith(prefix):
            queue.append(response["Location"])  # e.g. a CSI member: dashboard -> CSI file
        if response.status_code != 200 or "text/html" not in response.get("Content-Type", ""):
            continue
        page = response.content.decode()
        _assert_csp_clean(page, f"{role} {url}")
        if "<html" in page:
            assert nav_keys(page) == expected_nav, (role, url)
            sidebar = re.search(r'<nav id="sidebar".*?</nav>', page, re.S)
            assert sidebar is not None, (role, url)
            assert len(re.findall(r'aria-current="page"', sidebar.group(0))) <= 1, (role, url)
            _assert_structure(page, prefix, (role, url))
            if crowded is not None:
                crowded.extend((role.value, url, b) for b in _crowded_blocks(page))
            tabs = re.search(r'<nav class="tabs".*?</nav>', page, re.S)
            if tabs is not None:
                assert len(re.findall(r'aria-current="page"', tabs.group(0))) <= 1, (role, url)
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
    crowded: list[object] = []  # one primary button per form and per block (review B)
    for role in ROLES:
        user = Membership.objects.active().get(thesis=thesis, role=role).user
        enroll_totp(user)  # demo accounts have no factor yet: the middleware would redirect
        client = Client()
        client.force_login(user)
        reached[role] = _crawl(client, prefix, prefix, role, crowded)
    assert crowded == [], "\n".join(map(str, crowded))
    # The crawl really went through the modules.
    assert len([u for u, s in reached[Role.PHD_STUDENT].items() if s == 200]) > 30
    for role in ROLES:
        assert any(s == 200 for s in reached[role].values()), role
