"""Daily e-mails of the meetings: the reminder the day before, the overdue action.

Run once a day by `manage.py meetings_reminders` (systemd timer, as the
administrative file's reminders, docs/DEPLOYMENT.md); running it again, or twice
at the same time, sends nothing twice (MeetingNotification's unique constraint).

- Computed on DATES in Paris (timezone.localdate()), never on hours: a meeting
  at 00:30 in Paris is reminded the day before in Paris, whatever UTC says.
- REMINDER: scheduled meetings of an open thesis taking place tomorrow, to their
  attendees who are still active internal members. Its event is (meeting, start):
  a meeting moved to another day is reminded again.
- ACTION_OVERDUE: open actions whose due date was yesterday (J+1, once), to their
  assignee if still an active internal member.
- The e-mail is a link only (CLAUDE.md rule 7); the broker sees the row id only.
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass

from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.accounts.models import Thesis, User

from .members import internal_member_ids
from .models import ActionItem, Meeting, MeetingNotification
from .notifications import Kind, _queue, requeue_failed

OPEN_THESIS = (Thesis.Status.SETUP, Thesis.Status.ACTIVE)
# Fixed namespace of the reminders' event ids (uuid5): never change it, or every
# reminder already sent would be sent again.
EVENT_NAMESPACE = uuid.UUID("6f3c2a4e-5b1d-4c8e-9a7f-0d2e4b6c8a10")


@dataclass(frozen=True)
class RunResult:
    reminders: int
    overdue: int
    requeued: int


def reminder_event(meeting: Meeting) -> uuid.UUID:
    return uuid.uuid5(EVENT_NAMESPACE, f"reminder:{meeting.pk}:{meeting.starts_at.isoformat()}")


def overdue_event(action: ActionItem) -> uuid.UUID:
    return uuid.uuid5(EVENT_NAMESPACE, f"overdue:{action.pk}:{action.due_date.isoformat()}")


def _record_once(meeting: Meeting, kind: str, event: uuid.UUID, user: User) -> bool:
    """Create and queue the row unless it exists (unique constraint): True if new."""
    try:
        with transaction.atomic():
            row = MeetingNotification.objects.create(
                meeting=meeting, kind=kind, event_id=event, recipient=user
            )
            _queue(row)
    except IntegrityError:  # already recorded, by this run's twin or an earlier one
        return False
    return True


def _paris_day_bounds(day: dt.date) -> tuple[dt.datetime, dt.datetime]:
    start = timezone.make_aware(dt.datetime.combine(day, dt.time()))
    end = timezone.make_aware(dt.datetime.combine(day + dt.timedelta(days=1), dt.time()))
    return start, end


def run(today: dt.date | None = None) -> RunResult:
    if isinstance(today, dt.datetime):
        raise TypeError("the reminders are computed on dates only")
    today = today or timezone.localdate()
    start, end = _paris_day_bounds(today + dt.timedelta(days=1))
    reminders = 0
    meetings = Meeting.objects.filter(
        status=Meeting.Status.SCHEDULED,
        starts_at__gte=start,
        starts_at__lt=end,
        thesis__status__in=OPEN_THESIS,
    ).select_related("thesis")
    for meeting in meetings.order_by("starts_at", "pk"):
        members = internal_member_ids(meeting.thesis_id)
        attendees = User.objects.filter(
            pk__in=meeting.attendees.values_list("user_id", flat=True)
        ).order_by("pk")
        for user in attendees:
            if user.pk in members:
                reminders += _record_once(meeting, Kind.REMINDER, reminder_event(meeting), user)
    overdue = 0
    actions = ActionItem.objects.filter(
        status=ActionItem.Status.OPEN,
        due_date=today - dt.timedelta(days=1),
        thesis__status__in=OPEN_THESIS,
    ).select_related("meeting", "thesis", "assignee")
    for action in actions.order_by("pk"):
        if action.assignee_id in internal_member_ids(action.thesis_id):
            overdue += _record_once(
                action.meeting, Kind.ACTION_OVERDUE, overdue_event(action), action.assignee
            )
    return RunResult(reminders=reminders, overdue=overdue, requeued=requeue_failed())
