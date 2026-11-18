"""The look of the bell and of the Activity page (M13a review): unread rows are
marked by more than a colour (a dot, a tint, and a text for screen readers), each
row is ONE link (module icon, title on one line, details and a relative date),
rows are grouped « Nouveau » / « Plus tôt », the count stops at « 99+ », and
« Tout marquer comme lu » is reachable from the bell."""

from __future__ import annotations

import datetime as dt
import re
from types import SimpleNamespace
from typing import Any

import pytest

from apps.activity.tests import helpers as h
from apps.core.confidentiality import ConfidentialityLevel as L

pytestmark = pytest.mark.django_db(transaction=True)

BELL = re.compile(r'<div class="bell"[^>]*>.*?</div>\s*<!-- /bell -->', re.S)
ROW = re.compile(r"<li\b[^>]*>(.*?)</li>", re.S)


@pytest.fixture(autouse=True)
def _now(time_machine: Any) -> None:
    time_machine.move_to(h.NOW, tick=True)


@pytest.fixture
def team() -> SimpleNamespace:
    return h.make_team()


def _page(team: SimpleNamespace, user: Any) -> str:
    return h.client_for(user).get(h.url(team, "feed")).content.decode()


def _bell(html: str) -> str:
    match = BELL.search(html)
    assert match is not None, "no bell in the page"
    return match.group(0)


def _main(html: str) -> str:
    return html.split("<main", 1)[1].split("</main>", 1)[0]


def _rows(fragment: str) -> list[str]:
    return [r for r in ROW.findall(fragment) if "activity-row" in r]


def _two_read_one_unread(team: SimpleNamespace) -> None:
    from apps.activity.services import mark_all_read

    h.document(team, L.PUBLIC, title="Note lue fictive")
    h.document(team, L.PUBLIC, title="Autre note lue fictive")
    mark_all_read(team.director, team.thesis)
    h.document(team, L.PUBLIC, title="Note nouvelle fictive")


@pytest.mark.parametrize("where", ["bell", "page"])
def test_an_unread_row_is_marked_beyond_its_colour(team: SimpleNamespace, where: str) -> None:
    _two_read_one_unread(team)
    html = _page(team, team.director)
    fragment = _bell(html) if where == "bell" else _main(html)
    rows = _rows(fragment)
    assert len(rows) == 3
    unread = [r for r in rows if "is-unread" in r]
    assert len(unread) == 1 and "Note nouvelle fictive" in unread[0]
    assert 'class="visually-hidden">Non lu' in unread[0]  # said to a screen reader
    assert all("Non lu" not in r for r in rows if r not in unread)


@pytest.mark.parametrize("where", ["bell", "page"])
def test_a_row_is_one_link_with_its_icon_title_and_details(
    team: SimpleNamespace, where: str
) -> None:
    h.document(team, L.PUBLIC, title="Note fictive")
    html = _page(team, team.director)
    (row,) = _rows(_bell(html) if where == "bell" else _main(html))
    assert row.count("<a ") == 1  # the whole row, never a string of links
    assert re.search(r'<a [^>]*class="[^"]*activity-row[^"]*"[^>]*data-module="documents"', row)
    assert '<svg class="icon"' in row
    assert re.search(r'<span class="activity-title">.*Nouveau document.*</span>', row, re.S)
    assert re.search(r'<span class="activity-meta">Note fictive · <time datetime="', row)


def test_the_date_is_relative_then_a_date(team: SimpleNamespace, time_machine: Any) -> None:
    """Within a week: « il y a … »; older: the short date (« 20 janv. »)."""
    h.document(team, L.PUBLIC, title="Note ancienne fictive")
    time_machine.move_to(h.NOW + dt.timedelta(days=9), tick=True)
    h.document(team, L.PUBLIC, title="Note du jour fictive")
    time_machine.move_to(h.NOW + dt.timedelta(days=9, minutes=5), tick=True)
    page = _main(_page(team, team.director))
    assert re.search(r"Note du jour fictive · <time[^>]*>il y a 5\u00a0minutes<", page)
    assert re.search(r"Note ancienne fictive · <time[^>]*>20\u00a0janv\.<", page)


@pytest.mark.parametrize("where", ["bell", "page"])
def test_rows_are_grouped_new_then_earlier(team: SimpleNamespace, where: str) -> None:
    _two_read_one_unread(team)
    html = _page(team, team.director)
    fragment = _bell(html) if where == "bell" else _main(html)
    new, earlier = fragment.index(">Nouveau<"), fragment.index(">Plus tôt<")
    assert new < fragment.index("Note nouvelle fictive") < earlier
    assert earlier < fragment.index("Note lue fictive")


def test_no_group_heading_without_rows(team: SimpleNamespace) -> None:
    from apps.activity.services import mark_all_read

    h.document(team, L.PUBLIC, title="Note fictive")
    mark_all_read(team.director, team.thesis)
    bell = _bell(_page(team, team.director))
    assert ">Nouveau<" not in bell and ">Plus tôt<" in bell


def test_the_count_stops_at_99_plus(team: SimpleNamespace) -> None:
    from apps.activity.events import record

    document = h.document(team, L.PUBLIC)  # one event
    for _n in range(100):  # and a hundred more
        record(kind="DOCUMENT_VERSION_ADDED", obj=document, object_type="DOCUMENT",
               thesis=team.thesis, actor=team.student)  # fmt: skip
    bell = _bell(_page(team, team.director))
    assert re.search(r'class="bell-count"[^>]*>\s*99\+\s*<', bell)
    assert re.search(r'aria-label="[^"]*101 non lus"', bell)  # the exact number, said


def test_no_count_when_everything_is_read(team: SimpleNamespace) -> None:
    from apps.activity.services import mark_all_read

    h.document(team, L.PUBLIC, title="Note fictive")
    mark_all_read(team.director, team.thesis)
    assert 'class="bell-count"' not in _bell(_page(team, team.director))


def test_everything_is_marked_read_from_the_bell(team: SimpleNamespace) -> None:
    from apps.activity.models import EventRead

    h.document(team, L.PUBLIC, title="Note fictive")
    browser = h.browser_for(team.director)
    bell = _bell(browser.get(h.url(team, "preferences")).content.decode())
    assert f'action="{h.url(team, "read_all")}"' in bell
    assert "Tout marquer comme lu" in bell
    response = h.browser_post(browser, h.url(team, "preferences"), h.url(team, "read_all"), {})
    assert response.status_code == 302
    assert EventRead.objects.filter(user=team.director).exists()


def test_the_page_offers_the_same_and_one_filled_button_at_most(team: SimpleNamespace) -> None:
    h.document(team, L.PUBLIC, title="Note fictive")
    main = _main(_page(team, team.director))
    assert "Tout marquer comme lu" in main
    assert main.count("btn-primary") <= 1


TIME = re.compile(r'<time datetime="([^"]+)">([^<]*)</time>')


@pytest.mark.parametrize("where", ["bell", "page"])
def test_every_time_says_exactly_its_own_datetime(
    team: SimpleNamespace, where: str, time_machine: Any
) -> None:
    """The text of each <time> is the relative date of its datetime attribute and
    nothing else (no other value can slip in)."""
    from django.utils.dateparse import parse_datetime

    from apps.ui.templatetags.ui import ago

    final = h.NOW + dt.timedelta(days=9)
    for before, title in (
        (dt.timedelta(days=9), "Note A"),  # beyond a week: the short date
        (dt.timedelta(days=2), "Note B"),
        (dt.timedelta(hours=3), "Note C"),
        (dt.timedelta(minutes=7), "Note D"),
    ):
        time_machine.move_to(final - before, tick=False)
        h.document(team, L.PUBLIC, title=f"{title} fictive")
    time_machine.move_to(final, tick=False)
    html = _page(team, team.director)
    fragment = _bell(html) if where == "bell" else _main(html)
    times = TIME.findall(fragment)
    assert len(times) == 4
    shown = {text for _attr, text in times}
    assert len(shown) == 4  # four different texts: minutes, hours, days, a date
    for attribute, text in times:
        instant = parse_datetime(attribute)
        assert instant is not None, attribute
        assert text == ago(instant), (attribute, text)
    assert fragment.count("<time") == len(times)  # no <time> of another shape
