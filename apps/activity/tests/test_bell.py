"""The bell of the header (M11a): on every page of a thesis, the number of unread
elements and the 8 latest ones the viewer reads; filter first, cut afterwards; a
capped number of queries whatever the number of events (requirement c); no sealed
text opened, neither by the bell nor by the Activity page.
"""

from __future__ import annotations

import re
from types import SimpleNamespace
from typing import Any

import pytest
from django.urls import reverse

from apps.activity.tests import helpers as h
from apps.core.confidentiality import ConfidentialityLevel as L

pytestmark = pytest.mark.django_db(transaction=True)

BELL = re.compile(r'<div class="bell"[^>]*>.*?</div>\s*<!-- /bell -->', re.S)


@pytest.fixture(autouse=True)
def _now(time_machine: Any) -> None:
    time_machine.move_to(h.NOW, tick=True)


@pytest.fixture
def team() -> SimpleNamespace:
    return h.make_team()


def _bell(html: str) -> str:
    match = BELL.search(html)
    assert match is not None, "no bell in the page"
    return match.group(0)


@pytest.mark.parametrize(
    "name",
    ["accounts:dashboard", "accounts:team", "objectives:dashboard", "meetings:list",
     "vault:documents", "travel:calendar", "activity:feed"],
)  # fmt: skip
def test_the_bell_is_on_every_page_of_a_thesis(team: SimpleNamespace, name: str) -> None:
    html = (
        h.client_for(team.director)
        .get(reverse(name, kwargs={"thesis_id": team.thesis.pk}))
        .content.decode()
    )
    assert h.url(team, "feed") in _bell(html)


def test_no_bell_outside_a_thesis_nor_on_a_refused_page(team: SimpleNamespace) -> None:
    other = h.make_team()
    refused = h.client_for(team.director).get(h.url(other, "feed"))
    assert 'class="bell"' not in refused.content.decode()
    home = h.client_for(team.director).get(reverse("core:home")).content.decode()
    assert 'class="bell"' not in home


def test_the_count_and_the_latest_are_the_viewers(team: SimpleNamespace) -> None:
    h.document(team, L.INTERNAL_LIRMM, title="Lisible par le co-encadrant")
    for n in range(12):  # newer, unreadable by the LIRMM co-supervisor
        h.document(team, L.INTERNAL_ST, title=f"Caché {n}")
    bell = _bell(h.client_for(team.co_supervisor).get(h.url(team, "feed")).content.decode())
    assert "Lisible par le co-encadrant" in bell
    assert "Caché" not in bell
    assert re.search(r'class="bell-count"[^>]*>\s*1\s*<', bell)


def test_eight_latest_after_the_filter(team: SimpleNamespace) -> None:
    for n in range(10):
        h.document(team, L.INTERNAL_LIRMM, title=f"Lisible {n}")
    for n in range(10):
        h.document(team, L.INTERNAL_ST, title=f"Caché {n}")
    bell = _bell(h.client_for(team.co_supervisor).get(h.url(team, "feed")).content.decode())
    assert bell.count('class="bell-item"') == 8
    assert "Caché" not in bell


def test_the_bell_has_a_capped_number_of_queries(
    team: SimpleNamespace, django_assert_max_num_queries: Any
) -> None:
    """Hundreds of events, most unreadable: the bell costs the same."""
    from apps.activity.services import bell

    for n in range(30):
        h.document(team, L.INTERNAL_ST, title=f"Caché {n}")
    for n in range(5):
        h.document(team, L.INTERNAL_LIRMM, title=f"Lisible {n}")
    for _ in range(4):
        for level in L:
            h.meeting(team, level)
            h.objective(team, level)
    h.mission(team, "ROUSSET", "ST", start=h.D(2027, 2, 9))
    h.mission(team, "LIRMM", "LIRMM", start=h.D(2027, 2, 10))
    with django_assert_max_num_queries(12):
        shown = bell(team.co_supervisor, team.thesis)
        assert shown.unread >= 5
        assert len(shown.latest) == 8


def test_no_sealed_text_read_by_the_bell_nor_the_feed(team: SimpleNamespace) -> None:
    from apps.vault.models import AuditEvent

    session = h.meeting(team, L.PUBLIC, attendees=[team.student, team.director])
    from apps.meetings import services as meetings

    meetings.add_action(meeting=session, user=team.student, title="TEMOIN-FUITE-ACTION",
                        assignee=team.director, due_date=h.D(2027, 2, 15))  # fmt: skip
    h.submission(team, L.PUBLIC)
    h.objective(team, L.PUBLIC)
    h.mission(team, "LIRMM", "LIRMM", start=h.D(2027, 2, 9))
    before = AuditEvent.objects.filter(action="SEALED_TEXT_READ").count()
    html = h.client_for(team.director).get(h.url(team, "feed")).content.decode()
    assert AuditEvent.objects.filter(action="SEALED_TEXT_READ").count() == before
    for witness in ("TEMOIN-FUITE-ACTION", "TEMOIN-FUITE-TITRE-REUNION",
                    "TEMOIN-FUITE-TITRE-ARTICLE", "TEMOIN-FUITE-TITRE-OBJECTIF",
                    "TEMOIN-FUITE-MOTIF-MISSION"):  # fmt: skip
        assert witness not in html, witness
