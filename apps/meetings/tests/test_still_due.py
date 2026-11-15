"""Send-time check of the meetings' e-mails (branch notifications-still-due).

Under the row's lock, a MeetingNotification is sent only if the thesis is open,
the recipient is still concerned (active internal member, still an attendee or
the action's assignee, reading the meeting's level) and the meeting still calls
for it; otherwise SKIPPED, nothing sent, logged with the row id and a code only.

The meetings come from the services (sealed texts: transaction=True); the rows
under test are created directly, then the state is changed before sending.
"""

from __future__ import annotations

import datetime as dt
import logging
import uuid
from types import SimpleNamespace
from typing import Any

import pytest
from django.core import mail
from django.utils import timezone

from apps.accounts.models import Membership, Thesis
from apps.accounts.roles import Role
from apps.accounts.tests.factories import MembershipFactory
from apps.meetings import notifications, reminders, services, tasks
from apps.meetings.models import ActionItem, Attendee, Meeting, MeetingNotification
from apps.meetings.tests.helpers import START, L, make_team, meeting_of

pytestmark = pytest.mark.django_db(transaction=True)

K = MeetingNotification.Kind
LOGGER = "apps.core.delivery"
TOMORROW = START + dt.timedelta(days=1)


@pytest.fixture(autouse=True)
def _after_the_start(time_machine: Any) -> None:
    time_machine.move_to(START + dt.timedelta(hours=1), tick=True)


@pytest.fixture
def team() -> SimpleNamespace:
    return make_team()


@pytest.fixture
def log(caplog: pytest.LogCaptureFixture) -> pytest.LogCaptureFixture:
    caplog.set_level(logging.INFO, logger=LOGGER)
    return caplog


# --------------------------------------------------------------------------- helpers


def _row(meeting: Meeting, kind: str, user: Any, event: uuid.UUID | None = None) -> Any:
    return MeetingNotification.objects.create(
        meeting=meeting, kind=kind, event_id=event or uuid.uuid4(), recipient=user
    )


def _send(row: MeetingNotification) -> str:
    mail.outbox.clear()
    tasks.send_meeting_notification(str(row.pk))
    row.refresh_from_db()
    return str(row.status)


def _codes(log: Any) -> list[str]:
    return [r.getMessage() for r in log.records if r.name == LOGGER]


def _skipped(row: MeetingNotification, code: str, log: Any) -> None:
    log.clear()
    assert _send(row) == "SKIPPED", row.kind
    assert mail.outbox == []
    assert _codes(log) == [f"meetings notification skipped ({row.pk}, {code})"]


def _future(team: SimpleNamespace) -> Meeting:
    return meeting_of(team, starts_at=TOMORROW)


def _with_minutes(team: SimpleNamespace) -> Meeting:
    meeting = meeting_of(team)
    services.save_minutes(meeting=meeting, user=team.student, minutes="CR fictif", version=0)
    return Meeting.objects.get(pk=meeting.pk)


def _action(team: SimpleNamespace, meeting: Meeting | None = None) -> ActionItem:
    return services.add_action(
        meeting=meeting or meeting_of(team),
        user=team.student,
        title="Action fictive",
        assignee=team.co_supervisor,
        due_date=dt.date(2027, 1, 11),
    )


def _cancelled(team: SimpleNamespace) -> Meeting:
    return services.cancel_meeting(meeting=_future(team), user=team.student)


def _end(user: Any, thesis: Thesis) -> None:
    Membership.objects.filter(user=user, thesis=thesis).update(active_until=timezone.now())


def _one_of_each(team: SimpleNamespace) -> list[MeetingNotification]:
    """One row of each kind to the co-supervisor, each still due."""
    future = _future(team)
    action = _action(team)
    return [
        _row(future, K.INVITED, team.co_supervisor),
        _row(future, K.CHANGED, team.co_supervisor),
        _row(future, K.REMINDER, team.co_supervisor, reminders.reminder_event(future)),
        _row(_with_minutes(team), K.MINUTES, team.co_supervisor),
        _row(_cancelled(team), K.CANCELLED, team.co_supervisor),
        _row(
            Meeting.objects.get(pk=action.meeting_id),
            K.ACTION_OVERDUE,
            team.co_supervisor,
            reminders.overdue_event(action),
        ),
    ]


# --------------------------------------------------------------------------- every kind


def test_every_kind_has_a_case_here() -> None:
    assert set(K.values) == {
        "INVITED",
        "CHANGED",
        "CANCELLED",
        "MINUTES",
        "REMINDER",
        "ACTION_OVERDUE",
    }


def test_each_kind_still_due_is_sent(team: SimpleNamespace) -> None:
    for row in _one_of_each(team):
        assert _send(row) == "SENT", row.kind
        assert [m.to for m in mail.outbox] == [[team.co_supervisor.email]]


@pytest.mark.parametrize("status", [Thesis.Status.DEFENDED, Thesis.Status.CLOSED])
def test_a_finished_thesis_skips_every_kind(team: SimpleNamespace, status: str, log: Any) -> None:
    rows = _one_of_each(team)
    Thesis.objects.filter(pk=team.thesis.pk).update(status=status)
    for row in rows:
        _skipped(row, "thesis_closed", log)


def test_an_ended_membership_with_an_active_account_skips_every_kind(
    team: SimpleNamespace, log: Any
) -> None:
    rows = _one_of_each(team)
    _end(team.co_supervisor, team.thesis)
    for row in rows:
        _skipped(row, "not_concerned", log)


def test_a_level_no_longer_readable_skips_every_kind(team: SimpleNamespace, log: Any) -> None:
    """The co-supervisor does not read INTERNAL_ST (a level changed outside the
    services' attendee check, or a role changed meanwhile)."""
    rows = _one_of_each(team)
    Meeting.objects.filter(thesis=team.thesis).update(level=L.INTERNAL_ST)
    for row in rows:
        _skipped(row, "not_concerned", log)


def test_a_change_of_role_to_a_reader_of_the_level_keeps_the_e_mail(
    team: SimpleNamespace,
) -> None:
    """A new Membership row (another internal role) keeps the attendance."""
    row = _row(_future(team), K.INVITED, team.co_supervisor)
    _end(team.co_supervisor, team.thesis)
    MembershipFactory.create(thesis=team.thesis, user=team.co_supervisor, role=Role.DIRECTOR)
    assert _send(row) == "SENT"


def test_a_change_of_role_to_an_external_role_skips(team: SimpleNamespace, log: Any) -> None:
    row = _row(_future(team), K.INVITED, team.co_supervisor)
    _end(team.co_supervisor, team.thesis)
    MembershipFactory.create(thesis=team.thesis, user=team.co_supervisor, role=Role.GUEST)
    _skipped(row, "not_concerned", log)


# --------------------------------------------------------------------------- attendance


@pytest.mark.parametrize("kind", [K.INVITED, K.CHANGED, K.REMINDER, K.MINUTES])
def test_a_removed_attendee_is_skipped(team: SimpleNamespace, kind: str, log: Any) -> None:
    meeting = _with_minutes(team) if kind == K.MINUTES else _future(team)
    event = reminders.reminder_event(meeting) if kind == K.REMINDER else None
    row = _row(meeting, kind, team.co_supervisor, event)
    Attendee.objects.filter(meeting=meeting, user=team.co_supervisor).delete()
    _skipped(row, "not_concerned", log)


# --------------------------------------------------------------------------- meeting state


@pytest.mark.parametrize("kind", [K.INVITED, K.CHANGED, K.REMINDER, K.MINUTES])
def test_a_meeting_cancelled_meanwhile_is_skipped(
    team: SimpleNamespace, kind: str, log: Any
) -> None:
    meeting = _with_minutes(team) if kind == K.MINUTES else _future(team)
    event = reminders.reminder_event(meeting) if kind == K.REMINDER else None
    row = _row(meeting, kind, team.co_supervisor, event)
    services.cancel_meeting(meeting=meeting, user=team.student)
    _skipped(row, "not_due", log)


def test_a_cancellation_stays_due(team: SimpleNamespace) -> None:
    """An accomplished event: a cancellation is final."""
    assert _send(_row(_cancelled(team), K.CANCELLED, team.director)) == "SENT"


def test_a_reminder_of_a_moved_meeting_is_skipped(team: SimpleNamespace, log: Any) -> None:
    meeting = _future(team)
    row = _row(meeting, K.REMINDER, team.co_supervisor, reminders.reminder_event(meeting))
    Meeting.objects.filter(pk=meeting.pk).update(starts_at=TOMORROW + dt.timedelta(hours=2))
    _skipped(row, "not_due", log)


def test_a_reminder_sent_after_the_start_is_skipped(
    team: SimpleNamespace, log: Any, time_machine: Any
) -> None:
    meeting = _future(team)
    row = _row(meeting, K.REMINDER, team.co_supervisor, reminders.reminder_event(meeting))
    time_machine.move_to(TOMORROW + dt.timedelta(minutes=5), tick=True)
    _skipped(row, "not_due", log)


def test_minutes_of_a_meeting_without_minutes_are_skipped(team: SimpleNamespace, log: Any) -> None:
    _skipped(_row(meeting_of(team), K.MINUTES, team.co_supervisor), "not_due", log)


# --------------------------------------------------------------------------- overdue actions


def _overdue_row(team: SimpleNamespace) -> tuple[ActionItem, MeetingNotification]:
    action = _action(team)
    meeting = Meeting.objects.get(pk=action.meeting_id)
    row = _row(meeting, K.ACTION_OVERDUE, team.co_supervisor, reminders.overdue_event(action))
    return action, row


@pytest.mark.parametrize("status", [ActionItem.Status.DONE, ActionItem.Status.DROPPED])
def test_an_action_closed_meanwhile_is_skipped(
    team: SimpleNamespace, status: str, log: Any
) -> None:
    action, row = _overdue_row(team)
    services.set_action_status(action=action, user=team.co_supervisor, status=status)
    _skipped(row, "not_due", log)


def test_an_action_with_a_moved_due_date_is_skipped(team: SimpleNamespace, log: Any) -> None:
    action, row = _overdue_row(team)
    ActionItem.objects.filter(pk=action.pk).update(due_date=dt.date(2027, 3, 1))
    _skipped(row, "not_due", log)


def test_a_reassigned_action_is_skipped(team: SimpleNamespace, log: Any) -> None:
    action, row = _overdue_row(team)
    ActionItem.objects.filter(pk=action.pk).update(assignee=team.director)
    _skipped(row, "not_due", log)


def test_an_overdue_e_mail_does_not_need_the_attendance(team: SimpleNamespace) -> None:
    """The assignee of an action is concerned as such, attendee or not."""
    action, row = _overdue_row(team)
    Attendee.objects.filter(meeting_id=action.meeting_id, user=team.co_supervisor).delete()
    assert _send(row) == "SENT"


# --------------------------------------------------------------------------- others


def test_a_skipped_row_queued_again_logs_once(team: SimpleNamespace, log: Any) -> None:
    row = _row(_future(team), K.INVITED, team.co_supervisor)
    _end(team.co_supervisor, team.thesis)
    _skipped(row, "not_concerned", log)
    assert _send(row) == "SKIPPED"
    assert len(_codes(log)) == 1


def test_the_log_holds_no_title_or_address(team: SimpleNamespace, log: Any) -> None:
    meeting = meeting_of(team, starts_at=TOMORROW, title="Titre fictif unique")
    row = _row(meeting, K.INVITED, team.co_supervisor)
    _end(team.co_supervisor, team.thesis)
    log.set_level(logging.DEBUG)
    _send(row)
    for leak in ("Titre fictif unique", team.co_supervisor.email, str(meeting.pk)):
        assert leak not in log.text


def test_an_unknown_kind_is_never_concerned(team: SimpleNamespace) -> None:
    row = _row(_future(team), K.INVITED, team.co_supervisor)
    row.kind = "UNKNOWN"  # in memory only: the database refuses it (CHECK)
    assert notifications.concerned(row) is False
    assert notifications.still_due(row) is False
