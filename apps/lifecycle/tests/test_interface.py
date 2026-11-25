"""The interface after the defence and the closure (M13a): a banner on every page
of the thesis, and no write button where the right is refused (the student never
lands on a generic refusal). Every page reachable from the dashboard is crawled."""

from __future__ import annotations

import re
from types import SimpleNamespace
from typing import Any
from urllib.parse import urljoin, urlsplit

import pytest
from django.urls import reverse

from apps.accounts.models import Thesis
from apps.accounts.roles import Role
from apps.core.confidentiality import ConfidentialityLevel as L
from apps.lifecycle.tests import helpers as h

pytestmark = pytest.mark.django_db(transaction=True)
SKIP = ("/download/", ".ics", "/export", "logout")
FORM = re.compile(r"<form\b[^>]*>", re.I)


def _world(team: SimpleNamespace) -> None:
    """One object of each module, made while the thesis is ACTIVE."""
    from apps.activity.tests import helpers as a
    from apps.objectives import services as objectives

    goal = a.objective(team, L.PUBLIC)
    objectives.approve_objective(objective=goal, user=team.director, recent_mfa=True)
    a.document(team, L.PUBLIC)
    a.submission(team, L.PUBLIC)
    a.meeting(team, L.PUBLIC, days=3, attendees=[team.student, team.director])
    a.mission(team, "LIRMM", "LIRMM", start=h.D(2027, 6, 20))
    a.item(team, "ACADEMIC", [Role.DIRECTOR])


def _main(html: str) -> str:
    return html.split("<main", 1)[1].split("</main>", 1)[0] if "<main" in html else ""


def _crawl(user: Any, thesis: Thesis) -> dict[str, list[str]]:
    """{page: [the action of each POST form in its main part]} of every page of
    the thesis reachable from its dashboard."""
    client = h.client_for(user)
    prefix = f"/t/{thesis.pk}/"
    todo = [reverse("accounts:dashboard", kwargs={"thesis_id": thesis.pk})]
    seen: set[str] = set()
    forms: dict[str, list[str]] = {}
    while todo and len(seen) < 250:
        url = todo.pop()
        if url in seen:
            continue
        seen.add(url)
        response = client.get(url)
        if response.status_code != 200 or "text/html" not in response.get("Content-Type", ""):
            continue
        html = response.content.decode()
        main = _main(html)
        posts = [
            re.search(r'action="([^"]*)"', tag).group(1) if 'action="' in tag else url  # type: ignore[union-attr]
            for tag in FORM.findall(main)
            if re.search(r'method="post"', tag, re.I)
        ]
        if posts:
            forms[url] = posts
        for href in re.findall(r'href="([^"#]+)', html):
            target = urlsplit(urljoin(url, href)).path
            if target.startswith(prefix) and not any(s in target for s in SKIP):
                todo.append(target)
    assert len(seen) > 20, seen  # not vacuous: the pages of every module
    return forms


def _offending(
    forms: dict[str, list[str]], allowed: tuple[str, ...], thesis: Thesis
) -> dict[str, list[str]]:
    prefix = f"/t/{thesis.pk}/"
    return {
        page: bad
        for page, actions in forms.items()
        if (bad := [a for a in actions if not a.startswith(tuple(prefix + p for p in allowed))])
    }


PERSONAL = ("activity/",)  # read marks, one's own preferences


@pytest.mark.parametrize("attr", ["student", "director"])
def test_a_closed_thesis_shows_no_write_form(attr: str, time_machine: Any) -> None:
    time_machine.move_to(h.NOW, tick=True)
    team = h.make_team()
    _world(team)
    h.closed(team, time_machine)
    forms = _crawl(getattr(team, attr), team.thesis)
    assert _offending(forms, PERSONAL, team.thesis) == {}


@pytest.mark.parametrize("attr", ["student", "director", "tutor"])
def test_a_defended_thesis_shows_the_open_modules_only(attr: str, time_machine: Any) -> None:
    time_machine.move_to(h.NOW, tick=True)
    team = h.make_team()
    _world(team)
    h.defended(team)
    forms = _crawl(getattr(team, attr), team.thesis)
    open_parts = (
        *PERSONAL,
        "documents/",
        "conferences/",
        "travel/",
        "team/",
        "lifecycle/",
        "portfolio/publications/",
    )  # M9b: the details, as the ST approvals
    assert _offending(forms, open_parts, team.thesis) == {}
    travel = [a for actions in forms.values() for a in actions if "/travel/" in a]
    assert all(re.search(r"/(expenses|receipts?)/", a) for a in travel), travel
    team_forms = [a for actions in forms.values() for a in actions if "/team/" in a]
    assert all(a.endswith("/remove/") for a in team_forms), team_forms


@pytest.mark.parametrize("status", ["DEFENDED", "CLOSED"])
def test_every_page_of_the_thesis_shows_the_banner(status: str, time_machine: Any) -> None:
    time_machine.move_to(h.NOW, tick=True)
    team = h.make_team()
    if status == "DEFENDED":
        h.defended(team)
        words = "Thèse soutenue le 1 juin 2027."
    else:
        h.closed(team, time_machine)
        words = "Thèse close le 2 septembre 2027."
    client = h.client_for(team.student)
    for name in ("accounts:dashboard", "vault:documents", "objectives:list", "travel:missions"):
        try:
            url = reverse(name, kwargs={"thesis_id": team.thesis.pk})
        except Exception:  # noqa: S112 (a name that does not exist: skip it)
            continue
        page = client.get(url).content.decode()
        assert words in page and h.url(team, "page") in page, name


def test_an_active_thesis_shows_no_banner(time_machine: Any) -> None:
    time_machine.move_to(h.NOW, tick=True)
    team = h.make_team()
    page = h.client_for(team.student).get(
        reverse("accounts:dashboard", kwargs={"thesis_id": team.thesis.pk})
    )
    assert "Thèse soutenue" not in page.content.decode()
    assert "Thèse close" not in page.content.decode()


# --------------------------------------------------------------------------- the banner by role

OPEN_WORDS = ("documents", "conférences", "accords ST", "justificatifs", "missions")


def _banner(team: SimpleNamespace, user: Any) -> str:
    # The Activity page: every member who sees the thesis reads it (the CSI too).
    page = (
        h.client_for(user)
        .get(reverse("activity:feed", kwargs={"thesis_id": team.thesis.pk}))
        .content.decode()
    )
    match = re.search(r'<p class="message message-warning" role="status">(.*?)</p>', page, re.S)
    assert match is not None, "no banner"
    return match.group(1)


@pytest.mark.parametrize("attr", ["student", "director", "co_supervisor", "tutor",
                                  "st_co_supervisor"])  # fmt: skip
def test_the_banner_names_the_open_modules_the_role_sees(attr: str, time_machine: Any) -> None:
    time_machine.move_to(h.NOW, tick=True)
    team = h.make_team()
    h.defended(team)
    banner = _banner(team, getattr(team, attr))
    assert "Thèse soutenue le 1 juin 2027." in banner
    assert "les documents" in banner and "les conférences et les accords ST" in banner
    assert "les justificatifs des missions commencées avant la soutenance" in banner


def test_the_csi_member_reads_no_module_they_do_not_see(time_machine: Any) -> None:
    time_machine.move_to(h.NOW, tick=True)
    team = h.make_team()
    h.defended(team)
    banner = _banner(team, team.csi)
    assert "Thèse soutenue le 1 juin 2027." in banner
    assert not [w for w in OPEN_WORDS if w in banner], banner
    assert "Ce que vous voyez de la thèse est en lecture seule." in banner


@pytest.mark.parametrize("status", ["DEFENDED", "CLOSED"])
def test_the_link_of_the_banner_never_breaks(status: str, time_machine: Any) -> None:
    time_machine.move_to(h.NOW, tick=True)
    team = h.make_team()
    if status == "DEFENDED":
        h.defended(team)
    else:
        h.closed(team, time_machine)
    assert f'<a class="nowrap" href="{h.url(team, "page")}">Fin de thèse</a>' in _banner(
        team, team.student
    )
