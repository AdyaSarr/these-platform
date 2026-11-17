"""What a feed holds (M12): one's meetings, the M3 pieces to file or to validate,
the M6 deadlines (their type only), the missions one sees, the objective due
dates; clear metadata or generic labels only, never a sealed text nor a label
typed by someone; UIDs of the existing exports; times in UTC; all-day dates."""

from __future__ import annotations

import datetime as dt
from types import SimpleNamespace
from typing import Any

import pytest
from django.core.cache import cache

from apps.accounts.roles import Role
from apps.core.confidentiality import ConfidentialityLevel as L
from apps.feeds.tests import helpers as h
from apps.vault.models import AuditEvent

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture(autouse=True)
def _clear_limits() -> None:
    cache.clear()


@pytest.fixture
def team(time_machine: Any) -> SimpleNamespace:
    time_machine.move_to(h.NOW, tick=True)
    return h.make_team()


def _ics(user: Any, team: SimpleNamespace) -> str:
    _feed, token = h.new_feed(user, team.thesis)
    response = h.fetch(token)
    assert response.status_code == 200
    return response.content.decode()


def _unfold(text: str) -> str:
    return text.replace("\r\n ", "")


def test_a_meeting_of_the_invitee_only(team: SimpleNamespace) -> None:
    session = h.meeting(team, L.PUBLIC, attendees=[team.student, team.director])
    director = _unfold(_ics(team.director, team))
    assert f"UID:{session.pk}@" in director  # the UID of the M5a export
    assert "SUMMARY:Réunion d'encadrement" in director
    assert "TEMOIN-FUITE" not in director
    assert f"/meetings/{session.pk}/" in director  # a link to its page
    assert str(session.pk) not in _ics(team.co_supervisor, team)  # not invited


def test_a_meeting_whose_level_is_not_read_is_absent(team: SimpleNamespace) -> None:
    session = h.meeting(team, L.INTERNAL_ST, attendees=[team.student, team.tutor])
    assert str(session.pk) in _ics(team.tutor, team)
    assert str(session.pk) not in _ics(team.director, team)


def test_m3_pieces_to_file_and_to_validate(team: SimpleNamespace) -> None:
    from apps.admin_file.models import RequiredItem
    from apps.admin_file.tests.factories import RequiredItemFactory

    todo = RequiredItemFactory.create(thesis=team.thesis, code="custom-0123456789ab",
                                      label="TEMOIN-FUITE-LIBELLE", due_date=dt.date(2027, 3, 1),
                                      mandatory=False)  # fmt: skip
    submitted = h.item(team, "ACADEMIC", [Role.DIRECTOR])
    RequiredItem.objects.filter(pk=submitted.pk).update(due_date=dt.date(2027, 3, 2))
    student, director = _unfold(_ics(team.student, team)), _unfold(_ics(team.director, team))
    assert f"item-{todo.pk}@" in student and "à déposer" in student
    assert f"item-{submitted.pk}@" in director and "à valider" in director
    assert f"item-{submitted.pk}@" not in student
    for text in (student, director):
        assert "TEMOIN-FUITE" not in text and "Pièce fictive" not in text
    assert "DTSTART;VALUE=DATE:20270301" in student


def test_m6_deadlines_have_their_type_only(team: SimpleNamespace) -> None:
    from apps.conferences.tests.helpers import deadline_of, edition_of

    paper = h.submission(team, L.PUBLIC)
    deadline = deadline_of(team, paper.edition)
    student = _unfold(_ics(team.student, team))
    assert f"deadline-{deadline.pk}@" in student
    assert paper.venue.acronym not in student  # never the conference
    assert edition_of is not None


def test_missions_as_the_role_sees_them(team: SimpleNamespace) -> None:
    lab = h.mission(team, "LIRMM", "LIRMM", start=h.D(2027, 2, 9))
    st = h.mission(team, "ROUSSET", "ST", start=h.D(2027, 2, 10))
    co_sup = _ics(team.co_supervisor, team)
    assert f"mission-{lab.pk}@" in co_sup and f"mission-{st.pk}@" not in co_sup
    ics = _ics(team.st_co_supervisor, team)
    assert f"mission-{st.pk}@" in ics and f"mission-{lab.pk}@" not in ics
    assert "TEMOIN-FUITE" not in co_sup + ics


def test_objective_due_dates_without_their_title(team: SimpleNamespace) -> None:
    from apps.objectives import services as objectives

    goal = h.objective(team, L.PUBLIC)
    goal = objectives.approve_objective(objective=goal, user=team.director, recent_mfa=True)
    student = _unfold(_ics(team.student, team))
    assert f"objective-{goal.pk}@" in student and "TEMOIN-FUITE" not in student
    assert "DTSTART;VALUE=DATE:20270630" in student


def test_never_a_sealed_text_read(team: SimpleNamespace) -> None:
    h.meeting(team, L.CONFIDENTIAL, attendees=[team.student, team.director])
    h.mission(team, "LIRMM", "LIRMM", start=h.D(2027, 2, 9))
    before = AuditEvent.objects.filter(action="SEALED_TEXT_READ").count()
    _ics(team.director, team)
    assert AuditEvent.objects.filter(action="SEALED_TEXT_READ").count() == before


def test_presence_days_never(team: SimpleNamespace) -> None:
    from apps.travel import services as travel

    travel.set_presence(thesis=team.thesis, user=team.student, first=h.D(2027, 1, 25),
                        last=h.D(2027, 1, 29), kind="LEAVE")  # fmt: skip
    text = _ics(team.student, team)
    assert "Congé" not in text and "LEAVE" not in text and "presence" not in text.lower()


def test_utc_and_no_vtimezone(team: SimpleNamespace) -> None:
    h.meeting(team, L.PUBLIC, attendees=[team.student, team.director])
    text = _ics(team.director, team)
    assert "VTIMEZONE" not in text and "TZID" not in text
    assert any(line.startswith("DTSTART:") and line.endswith("Z") for line in text.split("\r\n"))
