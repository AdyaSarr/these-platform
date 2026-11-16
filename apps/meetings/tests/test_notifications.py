"""E-mails of the meetings: a link only (rule 7), ids only through the broker,
sent once, reminders on Paris dates, overdue actions at J+1 once, retries."""

from __future__ import annotations

import datetime as dt
import smtplib
from io import StringIO
from types import SimpleNamespace
from typing import Any

import pytest
from django.core import mail
from django.core.management import call_command

from apps.accounts.models import Membership
from apps.meetings import reminders, services, tasks
from apps.meetings.models import Meeting, MeetingNotification
from apps.meetings.tests.helpers import START, make_team, marker, meeting_of

pytestmark = pytest.mark.django_db(transaction=True)

D = dt.date


@pytest.fixture(autouse=True)
def _after_the_start(time_machine: Any) -> None:
    time_machine.move_to(START + dt.timedelta(hours=1), tick=True)


@pytest.fixture
def team() -> SimpleNamespace:
    return make_team()


def _bodies() -> str:
    return "\n".join(f"{m.subject}\n{m.body}" for m in mail.outbox)


def test_an_invitation_is_a_link_only(team: SimpleNamespace) -> None:
    title, room = marker(), marker()
    meeting = meeting_of(team, title=title, location_detail=room)
    assert sorted(m.to[0] for m in mail.outbox) == sorted(
        [team.director.email, team.co_supervisor.email]
    )
    text = _bodies()
    assert f"/t/{team.thesis.pk}/meetings/{meeting.pk}/" in text
    for secret in (title, room, team.thesis.title, team.student.email, team.student.last_name):
        assert secret not in text
    assert all(not m.attachments and not getattr(m, "alternatives", []) for m in mail.outbox)
    assert set(MeetingNotification.objects.values_list("status", flat=True)) == {"SENT"}


def test_the_broker_sees_the_row_id_only(team: SimpleNamespace, monkeypatch: Any) -> None:
    sent: list[tuple[Any, ...]] = []
    monkeypatch.setattr(tasks.send_meeting_notification, "delay", lambda *a: sent.append(a))
    meeting_of(team)
    ids = {str(pk) for pk in MeetingNotification.objects.values_list("pk", flat=True)}
    assert {args for args in sent} == {(i,) for i in ids}


def test_a_notification_is_sent_once(team: SimpleNamespace) -> None:
    meeting_of(team)
    row = MeetingNotification.objects.first()
    assert row is not None
    count = len(mail.outbox)
    tasks.send_meeting_notification(str(row.pk))
    assert len(mail.outbox) == count


def test_a_failed_send_is_retried_by_the_daily_run(team: SimpleNamespace, monkeypatch: Any) -> None:
    def refuse(*args: Any, **kwargs: Any) -> None:
        raise smtplib.SMTPException("refused")

    monkeypatch.setattr("apps.core.delivery.send_mail", refuse)
    meeting_of(team, attendees=[team.student, team.director])
    row = MeetingNotification.objects.get()
    assert (row.status, row.attempts) == ("FAILED", 1)
    reminders.run(D(2026, 12, 1))
    reminders.run(D(2026, 12, 2))
    row.refresh_from_db()
    assert (row.status, row.attempts) == ("FAILED", 3)
    monkeypatch.undo()
    reminders.run(D(2026, 12, 3))  # the attempt limit is reached: nothing more
    row.refresh_from_db()
    assert row.attempts == 3


# --------------------------------------------------------------------------- reminders


def test_the_reminder_goes_the_day_before_in_paris(team: SimpleNamespace) -> None:
    # 2027-01-13 at 00:30 in Paris is still 2027-01-12 in UTC.
    meeting = meeting_of(team, starts_at=dt.datetime(2027, 1, 12, 23, 30, tzinfo=dt.UTC))
    mail.outbox.clear()
    assert reminders.run(D(2027, 1, 11)).reminders == 0
    assert reminders.run(D(2027, 1, 12)).reminders == 3  # every attendee, the author too
    assert reminders.run(D(2027, 1, 12)).reminders == 0  # run again: nothing twice
    rows = MeetingNotification.objects.filter(meeting=meeting, kind="REMINDER")
    assert {r.recipient for r in rows} == {team.student, team.director, team.co_supervisor}
    assert "demain" in mail.outbox[0].subject


def test_a_moved_meeting_is_reminded_again_a_cancelled_one_never(team: SimpleNamespace) -> None:
    meeting = meeting_of(team, attendees=[team.student])
    assert reminders.run(D(2027, 1, 11)).reminders == 1
    later = START + dt.timedelta(days=7)
    services.update_meeting(meeting=meeting, user=team.student, changes={"starts_at": later})
    assert reminders.run(D(2027, 1, 18)).reminders == 1
    cancelled = meeting_of(team, attendees=[team.student], starts_at=START + dt.timedelta(days=1))
    services.cancel_meeting(meeting=cancelled, user=team.student)
    assert reminders.run(D(2027, 1, 12)).reminders == 0


def test_a_former_member_gets_no_reminder(team: SimpleNamespace) -> None:
    meeting_of(team, attendees=[team.student, team.director])
    Membership.objects.filter(user=team.director).update(
        active_until=dt.datetime(2026, 11, 1, tzinfo=dt.UTC)
    )
    assert reminders.run(D(2027, 1, 11)).reminders == 1


def test_overdue_actions_at_j_plus_1_once(team: SimpleNamespace) -> None:
    meeting = meeting_of(team)
    action = services.add_action(
        meeting=meeting,
        user=team.student,
        title=marker(),
        assignee=team.tutor,
        due_date=D(2027, 2, 1),
    )
    mail.outbox.clear()
    assert reminders.run(D(2027, 2, 1)).overdue == 0
    assert reminders.run(D(2027, 2, 2)).overdue == 1
    assert reminders.run(D(2027, 2, 2)).overdue == 0
    assert reminders.run(D(2027, 2, 3)).overdue == 0
    (sent,) = mail.outbox
    assert sent.to == [team.tutor.email]
    assert "retard" in sent.subject
    services.set_action_status(action=action, user=team.tutor, status="DONE")


def test_reminders_refuse_a_datetime() -> None:
    with pytest.raises(TypeError):
        reminders.run(dt.datetime(2027, 1, 1))


def test_the_command_prints_counts_only(team: SimpleNamespace, time_machine: Any) -> None:
    meeting_of(team, title="Titre fictif à ne pas afficher")
    time_machine.move_to(dt.datetime(2027, 1, 11, 7, 0, tzinfo=dt.UTC))
    out = StringIO()
    call_command("meetings_reminders", stdout=out)
    text = out.getvalue()
    assert text.startswith("3 meeting reminder(s), 0 overdue action(s) queued")
    assert "Titre" not in text and "@" not in text
    assert Meeting.objects.count() == 1
