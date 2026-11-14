"""Database constraints of the meetings (M5a): every CHECK and UNIQUE is hit in SQL,
bypassing the services, and the acknowledgements are append only."""

from __future__ import annotations

import datetime as dt
from types import SimpleNamespace
from typing import Any

import pytest
from django.db import IntegrityError, transaction

from apps.accounts.roles import Role
from apps.accounts.tests.factories import MembershipFactory, ThesisFactory
from apps.meetings.models import (
    ActionItem,
    Attendee,
    Decision,
    HistoryError,
    Meeting,
    MeetingDocument,
    MeetingNotification,
    MinutesAcknowledgement,
)
from apps.vault import formats

pytestmark = pytest.mark.django_db

START = dt.datetime(2027, 1, 12, 13, 0, tzinfo=dt.UTC)


@pytest.fixture
def team() -> SimpleNamespace:
    thesis = ThesisFactory.create()
    student = MembershipFactory.create(thesis=thesis, role=Role.PHD_STUDENT).user
    return SimpleNamespace(thesis=thesis, student=student)


def _meeting(team: SimpleNamespace, **overrides: Any) -> Meeting:
    fields: dict[str, Any] = {
        "thesis": team.thesis,
        "kind": Meeting.Kind.SUPERVISION,
        "starts_at": START,
        "duration_minutes": 60,
        "location": Meeting.Location.VISIO,
        "level": "PUBLIC",
        "created_by": team.student,
    }
    fields.update(overrides)
    return Meeting.objects.create(**fields)


def _refused(create: Any) -> None:
    with pytest.raises(IntegrityError), transaction.atomic():
        create()


# --------------------------------------------------------------------------- Meeting


def test_a_valid_meeting_is_stored(team: SimpleNamespace) -> None:
    meeting = _meeting(team)
    assert meeting.status == Meeting.Status.SCHEDULED
    assert meeting.minutes_version == 0
    assert meeting.ics_sequence == 0
    assert meeting.ends_at == START + dt.timedelta(minutes=60)


def test_presentation_venue_iff_presentation(team: SimpleNamespace) -> None:
    _refused(lambda: _meeting(team, kind=Meeting.Kind.PRESENTATION))
    _refused(lambda: _meeting(team, presentation_venue=Meeting.Venue.TEAM_SEMINAR))
    _meeting(team, kind=Meeting.Kind.PRESENTATION, presentation_venue=Meeting.Venue.ST_MEETING)


@pytest.mark.parametrize(("minutes", "ok"), [(4, False), (5, True), (600, True), (601, False)])
def test_duration_bounds(team: SimpleNamespace, minutes: int, ok: bool) -> None:
    if ok:
        _meeting(team, duration_minutes=minutes)
    else:
        _refused(lambda: _meeting(team, duration_minutes=minutes))


@pytest.mark.parametrize("field", ["kind", "location", "level", "status", "presentation_venue"])
def test_unknown_codes_are_refused(team: SimpleNamespace, field: str) -> None:
    extra = {"kind": Meeting.Kind.PRESENTATION, "presentation_venue": Meeting.Venue.OTHER}
    _refused(lambda: _meeting(team, **{**extra, field: "BOGUS"}))


def test_cancelled_at_iff_cancelled(team: SimpleNamespace) -> None:
    _refused(lambda: _meeting(team, status=Meeting.Status.CANCELLED))
    _refused(lambda: _meeting(team, cancelled_at=START))
    _meeting(team, status=Meeting.Status.CANCELLED, cancelled_at=START)


def test_minutes_version_needs_its_date(team: SimpleNamespace) -> None:
    _refused(lambda: _meeting(team, minutes_version=1))
    _meeting(team, minutes_version=1, minutes_updated_at=START, minutes_updated_by=team.student)


# --------------------------------------------------------------------------- dependents


def test_one_attendance_per_user(team: SimpleNamespace) -> None:
    meeting = _meeting(team)
    Attendee.objects.create(meeting=meeting, user=team.student)
    _refused(lambda: Attendee.objects.create(meeting=meeting, user=team.student))


def test_acknowledgements(team: SimpleNamespace) -> None:
    meeting = _meeting(team)
    kinds = MinutesAcknowledgement.Kind
    ack = MinutesAcknowledgement.objects.create(
        meeting=meeting, user=team.student, minutes_version=1, kind=kinds.READ
    )
    MinutesAcknowledgement.objects.create(
        meeting=meeting, user=team.student, minutes_version=1, kind=kinds.APPROVED
    )
    MinutesAcknowledgement.objects.create(
        meeting=meeting, user=team.student, minutes_version=2, kind=kinds.READ
    )
    _refused(
        lambda: MinutesAcknowledgement.objects.create(
            meeting=meeting, user=team.student, minutes_version=1, kind=kinds.READ
        )
    )
    _refused(
        lambda: MinutesAcknowledgement.objects.create(
            meeting=meeting, user=team.student, minutes_version=0, kind=kinds.READ
        )
    )
    _refused(
        lambda: MinutesAcknowledgement.objects.create(
            meeting=meeting, user=team.student, minutes_version=3, kind="BOGUS"
        )
    )
    with pytest.raises(HistoryError):
        ack.save()
    with pytest.raises(HistoryError):
        ack.delete()
    with pytest.raises(HistoryError):
        MinutesAcknowledgement.objects.filter(pk=ack.pk).update(minutes_version=9)
    with pytest.raises(HistoryError):
        MinutesAcknowledgement.objects.filter(pk=ack.pk).delete()


def test_action_done_at_iff_done(team: SimpleNamespace) -> None:
    meeting = _meeting(team)

    def action(**kw: Any) -> ActionItem:
        return ActionItem.objects.create(
            meeting=meeting,
            thesis=team.thesis,
            assignee=team.student,
            due_date=dt.date(2027, 2, 1),
            **kw,
        )

    _refused(lambda: action(status=ActionItem.Status.DONE))
    _refused(lambda: action(done_at=START))
    _refused(lambda: action(status="BOGUS"))
    action(status=ActionItem.Status.DONE, done_at=START)
    assert action().status == ActionItem.Status.OPEN


def test_decision_belongs_to_a_meeting(team: SimpleNamespace) -> None:
    decision = Decision.objects.create(meeting=_meeting(team), thesis=team.thesis)
    assert decision.withdrawn_at is None


def test_one_link_per_document(team: SimpleNamespace) -> None:
    from apps.vault.models import Document

    document = Document.objects.create(
        thesis=team.thesis, title="Document fictif", confidentiality="PUBLIC", owner=team.student
    )
    meeting = _meeting(team)
    MeetingDocument.objects.create(meeting=meeting, document=document)
    _refused(lambda: MeetingDocument.objects.create(meeting=meeting, document=document))


def test_one_notification_per_event_and_recipient(team: SimpleNamespace) -> None:
    import uuid

    meeting = _meeting(team)
    event = uuid.uuid4()
    kind = MeetingNotification.Kind.INVITED
    MeetingNotification.objects.create(
        meeting=meeting, kind=kind, event_id=event, recipient=team.student
    )
    _refused(
        lambda: MeetingNotification.objects.create(
            meeting=meeting, kind=kind, event_id=event, recipient=team.student
        )
    )


# --------------------------------------------------------------------------- slots


MEETING_SLOTS = {
    "meetings.Meeting.title": 0x0201,
    "meetings.Meeting.agenda": 0x0202,
    "meetings.Meeting.minutes": 0x0203,
    "meetings.Meeting.location_detail": 0x0204,
    "meetings.Meeting.visio_link": 0x0205,
    "meetings.Decision.text": 0x0206,
    "meetings.ActionItem.title": 0x0207,
}


def test_sealed_slots_of_the_meetings() -> None:
    assert {k: v for k, v in formats.TEXT_SLOTS.items() if k.startswith("meetings.")} == (
        MEETING_SLOTS
    )


def test_no_text_column_on_the_meeting_tables() -> None:
    """Texts are sealed: no CharField/TextField can hold a title, agenda or minutes."""
    from django.db import models

    allowed = {
        "kind",
        "presentation_venue",
        "location",
        "level",
        "status",
        "user",
    }
    for model in (Meeting, Attendee, Decision, ActionItem, MinutesAcknowledgement):
        for field in model._meta.concrete_fields:
            if isinstance(field, models.CharField | models.TextField):
                assert field.choices or field.name in allowed, (model.__name__, field.name)
