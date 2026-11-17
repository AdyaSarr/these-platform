"""The "Travel" card of the thesis dashboard and the "Travel" section of a meeting (M5)
and of a submission (M6): each viewer sees what M7 lets them see (a co-supervisor:
LIRMM only, no split, no budget), never a purpose nor an amount they may not read."""

from __future__ import annotations

from html import unescape
from types import SimpleNamespace
from typing import Any

import pytest
from django.urls import reverse

from apps.conferences.tests.helpers import submission_of
from apps.meetings.tests.helpers import meeting_of
from apps.travel import services
from apps.travel.models import PresenceDay
from apps.travel.tests.helpers import NOW, D, client_for, make_team, marker, mission_of

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture(autouse=True)
def _now(time_machine: Any) -> None:
    time_machine.move_to(NOW, tick=False)


@pytest.fixture
def team() -> SimpleNamespace:
    return make_team()


def _dashboard(team: SimpleNamespace, role: str) -> str:
    response = client_for(team, role).get(
        reverse("accounts:dashboard", kwargs={"thesis_id": team.thesis.pk})
    )
    assert response.status_code == 200
    html = unescape(response.content.decode())
    start = html.find('data-block="travel"')
    return "" if start < 0 else html[start : html.find("</section>", start)]


def _page(team: SimpleNamespace, role: str) -> str:
    """The whole dashboard: the tiles carry the counts the card carried (step 8a)."""
    response = client_for(team, role).get(
        reverse("accounts:dashboard", kwargs={"thesis_id": team.thesis.pk})
    )
    assert response.status_code == 200
    return unescape(response.content.decode())


def _agenda(team: SimpleNamespace, role: str) -> str:
    """The « À venir » card: the next missions moved there (frontend step 8a)."""
    html = _page(team, role)
    start = html.find('<section class="card agenda"')
    assert start >= 0
    return html[start : html.find("</section>", start)]


# =========================================================================== card


@pytest.mark.parametrize("role", ["student", "director", "co_supervisor", "tutor"])
def test_the_card_is_for_the_supervision(team: SimpleNamespace, role: str) -> None:
    assert "Déplacements" in _dashboard(team, role)


def test_the_next_mission_without_its_purpose(team: SimpleNamespace) -> None:
    secret = marker()
    mission_of(
        team,
        destination="ROUSSET",
        purpose=secret,
        start_date=D(2027, 3, 1),
        end_date=D(2027, 3, 2),
    )
    mission_of(team, destination="LIRMM", start_date=D(2027, 2, 8), end_date=D(2027, 2, 9))
    agenda = _agenda(team, "student")
    assert "8\u00a0févr." in agenda and "LIRMM" in agenda
    assert secret not in _page(team, "student")


def test_the_co_supervisor_sees_the_next_lirmm_mission_only(team: SimpleNamespace) -> None:
    mission_of(team, destination="ROUSSET", start_date=D(2027, 1, 25), end_date=D(2027, 1, 25))
    mission_of(team, destination="LIRMM", start_date=D(2027, 2, 8), end_date=D(2027, 2, 9))
    agenda = _agenda(team, "co_supervisor")
    assert "8\u00a0févr." in agenda and "25\u00a0janv." not in agenda and "Rousset" not in agenda


def test_pending_approvals_for_the_decider(team: SimpleNamespace) -> None:
    order_mission = mission_of(team, payer="ST")
    services.request_order(mission=order_mission, user=team.student, estimated_total="1")
    tutor = _page(team, "tutor")
    assert "1 accord de mission à décider" in tutor
    # The negative looks for a text that exists: the same words on the tutor's page.
    assert "accord de mission à décider" in tutor
    assert "accord de mission à décider" not in _page(team, "director")


def test_the_split_alert_but_not_for_the_co_supervisor(team: SimpleNamespace) -> None:
    for day in (D(2027, 1, 4), D(2027, 1, 5), D(2027, 1, 6)):
        PresenceDay.objects.create(thesis=team.thesis, day=day, kind="ROUSSET")
    PresenceDay.objects.create(thesis=team.thesis, day=D(2027, 1, 7), kind="LIRMM")
    services.set_target(
        thesis=team.thesis, user=team.tutor, recent_mfa=True, cifre_year=1, st_percent=50
    )
    for role in ("student", "director", "tutor"):
        assert "Écart à la cible" in _dashboard(team, role), role
    card = _dashboard(team, "co_supervisor")
    assert "Écart" not in card and "Part ST" not in card
    assert "Jours au LIRMM" in card


def test_the_budget_for_its_readers_only(team: SimpleNamespace) -> None:
    services.set_budget(
        thesis=team.thesis, user=team.tutor, payer="ST", year=2027, amount="4000", recent_mfa=True
    )
    # The budgets left the dashboard (one sealed read per display, frontend step 8a):
    # no amount there for anyone; the Budget tab shows it to its reader only.
    for role in ("student", "director", "co_supervisor", "tutor"):
        assert "4\u00a0000,00" not in _page(team, role), role
    budget = reverse("travel:budget", kwargs={"thesis_id": team.thesis.pk}) + "?year=2027"
    tutor = client_for(team, "tutor").get(budget)
    assert tutor.status_code == 200 and "4\u00a0000,00\u00a0€" in unescape(tutor.content.decode())
    director = client_for(team, "director").get(budget)
    assert director.status_code == 200 and "4\u00a0000,00" not in unescape(
        director.content.decode()
    )
    assert client_for(team, "co_supervisor").get(budget).status_code == 403


def test_missing_receipts_for_the_student(team: SimpleNamespace, time_machine: Any) -> None:
    import datetime as dt

    mission = mission_of(team, payer="OTHER")
    time_machine.move_to(dt.datetime(2027, 2, 15, 9, 0, tzinfo=dt.UTC), tick=False)
    services.mark_done(mission=mission, user=team.student)
    services.add_expense(mission=mission, user=team.student, category="TRANSPORT", actual="10")
    assert "1 mission avec des justificatifs manquants" in _page(team, "student")


# =========================================================================== M5, M6


def _meeting_page(team: SimpleNamespace, role: str, meeting: Any) -> str:
    page = client_for(team, role).get(
        reverse("meetings:detail", kwargs={"thesis_id": team.thesis.pk, "meeting_id": meeting.pk})
    )
    return unescape(page.content.decode())


def test_a_meeting_lists_its_missions_and_offers_one(team: SimpleNamespace) -> None:
    meeting = meeting_of(team)
    lirmm = mission_of(team, destination="LIRMM", meeting=meeting)
    hidden = mission_of(team, destination="ROUSSET", meeting=meeting)
    student = _meeting_page(team, "student", meeting)
    assert str(lirmm.pk) in student and str(hidden.pk) in student
    assert f"?meeting={meeting.pk}" in student  # « Prévoir une mission »
    co_sup = _meeting_page(team, "co_supervisor", meeting)
    assert str(lirmm.pk) in co_sup and str(hidden.pk) not in co_sup
    assert f"?meeting={meeting.pk}" not in co_sup


def test_a_submission_lists_its_missions(team: SimpleNamespace) -> None:
    submission = submission_of(team)
    mission = mission_of(team, destination="CONFERENCE", payer="LIRMM", submission=submission)
    page = client_for(team, "tutor").get(
        reverse(
            "conferences:submission",
            kwargs={"thesis_id": team.thesis.pk, "submission_id": submission.pk},
        )
    )
    html = unescape(page.content.decode())
    assert "Déplacements" in html and str(mission.pk) in html
    assert f"?submission={submission.pk}" not in html  # the tutor plans no mission


def test_no_travel_section_outside_the_supervision(team: SimpleNamespace) -> None:
    # A CSI member reads no meeting: nothing to check there; a meeting page for a reader
    # without TRAVEL_READ does not exist today (MEETING_READ is the supervision's).
    meeting = meeting_of(team)
    assert "Déplacements" in _meeting_page(team, "director", meeting)
