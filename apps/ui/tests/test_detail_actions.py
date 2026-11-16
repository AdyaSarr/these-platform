"""Actions of a meeting's page, and the <details> confirmations everywhere.

- « Ajouter à mon agenda (.ics) », « Modifier », « Dupliquer » are discreet
  buttons with an icon in the page header, like the other modules; « Annuler la
  réunion » is there too, behind its confirmation.
- A confirmation opens BELOW its row, across the whole row, without moving the
  other actions: its panel is positioned (absolute) against the nearest
  .confirm-row (the page header, a team card's menu, an invitation's cell), or
  under its own button elsewhere. Checked in a browser on a probe page: the
  actions keep their place and the row its height when the panel opens.
"""

from __future__ import annotations

import datetime as dt
import html as html_module
import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from django.test import Client
from django.urls import reverse

pytestmark = pytest.mark.django_db(transaction=True)

BASE_DIR = Path(__file__).resolve().parents[3]


@pytest.fixture
def team(time_machine: Any) -> SimpleNamespace:
    from apps.meetings.tests.helpers import meeting_of
    from apps.travel.tests.helpers import make_team

    time_machine.move_to(dt.datetime(2027, 1, 5, 9, 0, tzinfo=dt.UTC), tick=False)
    team = make_team()
    team.meeting = meeting_of(team)
    return team


def _page(team: SimpleNamespace, role: str) -> str:
    client = Client()
    client.force_login(getattr(team, role))
    kwargs = {"thesis_id": team.thesis.pk, "meeting_id": team.meeting.pk}
    response = client.get(reverse("meetings:detail", kwargs=kwargs))
    assert response.status_code == 200
    return html_module.unescape(response.content.decode())


def _header(page: str) -> str:
    match = re.search(r'<header class="page-header confirm-row">.*?</header>', page, re.S)
    assert match is not None
    return match.group(0)


def _url(team: SimpleNamespace, name: str) -> str:
    return reverse(
        f"meetings:{name}", kwargs={"thesis_id": team.thesis.pk, "meeting_id": team.meeting.pk}
    )


def test_the_writer_gets_buttons_in_the_header(team: SimpleNamespace) -> None:
    page = _page(team, "student")
    header = _header(page)
    for name, icon in (("ics", "calendar-plus"), ("edit", "pencil"), ("duplicate", "copy")):
        from apps.ui.icons import svg

        url = _url(team, name)
        button = re.search(rf'<a class="btn btn-ghost" href="{url}">(.*?)</a>', header, re.S)
        assert button is not None, name
        assert str(svg(icon)) in button.group(1), name
        assert page.count(f'href="{url}"') == 1, name  # no underlined link left below
    assert '<details class="confirm">' in header and f'action="{_url(team, "cancel")}"' in header


def test_a_reader_gets_the_calendar_only(team: SimpleNamespace) -> None:
    header = _header(_page(team, "co_supervisor"))
    assert _url(team, "ics") in header
    for name in ("edit", "duplicate", "cancel"):
        assert _url(team, name) not in header, name


def _css() -> str:
    css = (BASE_DIR / "static" / "css" / "components.css").read_text(encoding="utf-8")
    return re.sub(r"/\*.*?\*/", "", css, flags=re.S)


def _rule(selector: str) -> str:
    rules = {sel.strip(): body for sel, body in re.findall(r"([^{}]+)\{([^{}]*)\}", _css())}
    return rules.get(selector, "")


def test_a_confirmation_panel_never_moves_its_row() -> None:
    assert re.search(r"position\s*:\s*absolute", _rule(".confirm-panel"))
    assert re.search(r"position\s*:\s*relative", _rule(".confirm"))
    assert re.search(r"position\s*:\s*relative", _rule(".confirm-row"))
    assert re.search(r"position\s*:\s*static", _rule(".confirm-row .confirm"))
    assert re.search(r"inset-inline\s*:\s*0", _rule(".confirm-row .confirm-panel"))


def test_every_confirmation_sits_in_a_row() -> None:
    """Each template that includes the confirmation gives it a .confirm-row."""
    page_header = (BASE_DIR / "templates" / "ui" / "page_header.html").read_text("utf-8")
    assert 'class="page-header confirm-row"' in page_header
    team = (BASE_DIR / "templates" / "accounts" / "team.html").read_text("utf-8")
    assert team.count('{% include "ui/confirm.html"') == 2
    assert 'class="menu-panel confirm-row"' in team
    assert 'class="actions confirm-row"' in team
