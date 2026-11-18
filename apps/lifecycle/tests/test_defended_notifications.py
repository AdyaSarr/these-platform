"""After the defence, the notifications of a module follow its right to write
(M13a, apps/accounts/statuses.py): M6 (ST approvals, their reminders, deadlines)
and the receipts of M7 keep their e-mails, reminders and digests; M4 and M5a stay
SKIPPED."""

from __future__ import annotations

import datetime as dt
from types import SimpleNamespace
from typing import Any

import pytest
from django.core import mail

from apps.accounts.models import Thesis
from apps.activity.preferences import set_preference
from apps.lifecycle.tests import helpers as h

pytestmark = pytest.mark.django_db(transaction=True)
UTC = dt.UTC


def _defend(team: SimpleNamespace, day: dt.date) -> None:
    Thesis.objects.filter(pk=team.thesis.pk).update(status=Thesis.Status.DEFENDED, defended_on=day)


def _immediate(team: SimpleNamespace, attr: str, *categories: str) -> None:
    for category in categories:
        set_preference(getattr(team, attr), team.thesis, category, "IMMEDIATE")


def _submission(team: SimpleNamespace) -> Any:
    from apps.conferences.tests.helpers import (
        article_version,
        deadline_of,
        edition_of,
        submission_of,
    )

    edition = edition_of(team)
    deadline_of(team, edition)  # 2027-03-01 23:59 AoE
    submission = submission_of(team, edition=edition)
    return submission, article_version(team, submission)


def test_an_st_approval_asked_after_the_defence_reaches_the_tutor(time_machine: Any) -> None:
    from apps.conferences import services
    from apps.conferences.models import ConferenceNotification

    time_machine.move_to(dt.datetime(2027, 1, 10, 9, 0, tzinfo=UTC), tick=True)
    team = h.make_team()
    _immediate(team, "tutor", "ACTION", "DEADLINE")
    submission, version = _submission(team)
    _defend(team, h.D(2027, 1, 8))
    mail.outbox.clear()
    services.request_approval(submission=submission, user=team.student, version=version)
    row = ConferenceNotification.objects.get(kind="APPROVAL_REQUESTED")
    assert (row.recipient, row.status) == (team.tutor, "SENT")
    assert [m.to for m in mail.outbox] == [[team.tutor.email]]


def test_its_reminder_goes_too(time_machine: Any) -> None:
    from apps.conferences import reminders, services
    from apps.conferences.models import ConferenceNotification

    time_machine.move_to(dt.datetime(2027, 1, 10, 9, 0, tzinfo=UTC), tick=True)
    team = h.make_team()
    _immediate(team, "tutor", "ACTION", "DEADLINE")
    submission, version = _submission(team)
    services.request_approval(submission=submission, user=team.student, version=version)
    _defend(team, h.D(2027, 1, 8))
    reminders.run(dt.datetime(2027, 2, 23, 6, 0, tzinfo=UTC))  # J-7
    (row,) = ConferenceNotification.objects.filter(kind="APPROVAL_PENDING")
    assert (row.recipient, row.status) == (team.tutor, "SENT")


def _done_mission(team: SimpleNamespace, time_machine: Any) -> Any:
    from apps.travel import services
    from apps.travel.tests.helpers import mission_of

    mission = mission_of(team, payer="OTHER")  # 8-10 February 2027
    time_machine.move_to(dt.datetime(2027, 2, 11, 9, 0, tzinfo=UTC), tick=True)
    services.mark_done(mission=mission, user=team.student)
    services.add_expense(mission=mission, user=team.student, category="TRANSPORT", actual="80")
    return mission


def test_a_missing_receipt_of_an_earlier_mission_is_reminded(time_machine: Any) -> None:
    from apps.travel import reminders
    from apps.travel.models import TravelNotification

    time_machine.move_to(dt.datetime(2027, 1, 20, 9, 0, tzinfo=UTC), tick=True)
    team = h.make_team()
    _immediate(team, "student", "DEADLINE")
    _done_mission(team, time_machine)
    _defend(team, h.D(2027, 2, 12))  # after the mission began
    reminders.run(dt.datetime(2027, 2, 13, 9, 0, tzinfo=UTC))  # 3 days after the return
    (row,) = TravelNotification.objects.filter(kind="RECEIPTS_MISSING")
    assert (row.recipient, row.status) == (team.student, "SENT")


def test_no_approval_reminder_of_m7_after_the_defence(time_machine: Any) -> None:
    from apps.travel import reminders
    from apps.travel.models import TravelNotification
    from apps.travel.tests.helpers import mission_of

    time_machine.move_to(dt.datetime(2027, 1, 20, 9, 0, tzinfo=UTC), tick=True)
    team = h.make_team()
    mission_of(team, payer="ST", start_date=h.D(2027, 2, 15), end_date=h.D(2027, 2, 16))
    _defend(team, h.D(2027, 1, 18))
    reminders.run(dt.datetime(2027, 2, 1, 6, 0, tzinfo=UTC))
    assert not TravelNotification.objects.filter(kind="ORDER_TO_REQUEST").exists()


def test_a_meeting_e_mail_stays_skipped(time_machine: Any) -> None:
    from apps.meetings import tasks
    from apps.meetings.models import MeetingNotification
    from apps.meetings.tests.helpers import meeting_of

    time_machine.move_to(dt.datetime(2027, 1, 10, 9, 0, tzinfo=UTC), tick=True)
    team = h.make_team()
    _immediate(team, "director", "DEADLINE")
    meeting_of(team, starts_at=dt.datetime(2027, 1, 20, 13, 0, tzinfo=UTC))
    row = MeetingNotification.objects.filter(recipient=team.director).first()
    assert row is not None
    MeetingNotification.objects.filter(pk=row.pk).update(status="PENDING", attempts=0)
    _defend(team, h.D(2027, 1, 8))
    tasks.send_meeting_notification(str(row.pk))
    assert MeetingNotification.objects.get(pk=row.pk).status == "SKIPPED"


def test_an_objective_e_mail_stays_skipped(time_machine: Any) -> None:
    from apps.activity import tasks
    from apps.activity.models import ActivityNotification
    from apps.activity.tests.helpers import objective
    from apps.core.confidentiality import ConfidentialityLevel

    time_machine.move_to(dt.datetime(2027, 1, 10, 9, 0, tzinfo=UTC), tick=True)
    team = h.make_team()
    _immediate(team, "director", "INFO")
    objective(team, ConfidentialityLevel.PUBLIC)
    row = ActivityNotification.objects.filter(recipient=team.director, kind="EVENT").first()
    assert row is not None and row.event is not None
    assert row.event.kind == "OBJECTIVE_CREATED"
    ActivityNotification.objects.filter(pk=row.pk).update(status="PENDING", attempts=0)
    _defend(team, h.D(2027, 1, 8))
    tasks.send_activity_notification(str(row.pk))
    assert ActivityNotification.objects.get(pk=row.pk).status == "SKIPPED"


def test_the_digest_keeps_m6_and_skips_m5a(time_machine: Any) -> None:
    from apps.activity import digests
    from apps.conferences import services
    from apps.conferences.models import ConferenceNotification
    from apps.meetings.models import MeetingNotification
    from apps.meetings.tests.helpers import meeting_of

    time_machine.move_to(dt.datetime(2027, 1, 10, 9, 0, tzinfo=UTC), tick=True)
    team = h.make_team()
    set_preference(team.tutor, team.thesis, "ACTION", "DAILY")
    set_preference(team.tutor, team.thesis, "DEADLINE", "DAILY")
    meeting_of(team, starts_at=dt.datetime(2027, 1, 20, 13, 0, tzinfo=UTC),
               attendees=[team.student, team.tutor])  # fmt: skip
    meeting_row = MeetingNotification.objects.get(recipient=team.tutor)
    assert meeting_row.status == "DEFERRED"
    submission, version = _submission(team)
    _defend(team, h.D(2027, 1, 8))
    services.request_approval(submission=submission, user=team.student, version=version)
    approval_row = ConferenceNotification.objects.get(kind="APPROVAL_REQUESTED")
    assert approval_row.status == "DEFERRED"
    mail.outbox.clear()
    digests.run(h.D(2027, 1, 11))
    assert ConferenceNotification.objects.get(pk=approval_row.pk).status == "DIGESTED"
    assert MeetingNotification.objects.get(pk=meeting_row.pk).status == "SKIPPED"
    # The tutor's digest (the others get one too: the M6 Informations of the day).
    assert [team.tutor.email] in [m.to for m in mail.outbox]


def test_every_kind_of_m6_is_still_sent_after_the_defence(time_machine: Any) -> None:
    """The DEFENDED case of conferences/tests/test_still_due.py (M13a)."""
    from apps.conferences.tests.helpers import NOW, make_team
    from apps.conferences.tests.test_still_due import _information_at_once, _one_of_each, _send

    time_machine.move_to(NOW, tick=True)
    team = make_team()
    _information_at_once(team)
    rows = _one_of_each(team)
    _defend(team, h.D(2027, 1, 8))
    assert len(rows) == 6  # every kind
    for row in rows:
        assert _send(row) == "SENT", row.kind
        assert [m.to for m in mail.outbox] == [[row.recipient.email]]
