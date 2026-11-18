"""Notifications of meetings: same mechanism as the administrative file (M3).

notify() runs INSIDE the caller's transaction, before its audit(): one
MeetingNotification per recipient, queued after commit with its id only (the
broker never sees an address, a title or a text). The e-mail is a link to the
meeting, nothing else (CLAUDE.md rule 7). A FAILED notification is queued again
by the daily `meetings_reminders` run, up to MAX_ATTEMPTS.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable

from django.db import transaction
from django.utils import timezone

from apps.accounts.models import User
from apps.accounts.permissions import can_access_level
from apps.accounts.statuses import is_open_for
from apps.activity import routing
from apps.core import delivery
from apps.core.confidentiality import ConfidentialityLevel

from .members import internal_member_ids, is_internal_member
from .models import ActionItem, Attendee, Meeting, MeetingNotification, NotificationKind

Kind = MeetingNotification.Kind
MAX_ATTEMPTS = 3
ATTENDEE_KINDS = frozenset(
    {Kind.INVITED, Kind.CHANGED, Kind.CANCELLED, Kind.MINUTES, Kind.REMINDER}
)

# --------------------------------------------------------------------------- send-time check
# Checked by the sending task under the row's lock (apps.core.delivery): the
# state may have changed since the row was queued.


def concerned(row: MeetingNotification) -> bool:
    """An active internal member reading the meeting's level; an attendee, except
    for an overdue action (its assignee, checked by still_due)."""
    user, meeting = row.recipient, row.meeting
    if not is_internal_member(user, meeting.thesis_id):
        return False
    if not can_access_level(user, meeting.thesis, ConfidentialityLevel(meeting.level)):
        return False
    if row.kind == Kind.ACTION_OVERDUE:
        return True
    if row.kind in ATTENDEE_KINDS:
        return Attendee.objects.filter(meeting=meeting, user_id=user.pk).exists()
    return False


def still_due(row: MeetingNotification) -> bool:
    """The meeting still calls for it: not cancelled (invitation, change, minutes
    written); a reminder of the same start, before it; an overdue action still
    open, of the same due date, still assigned to the recipient. A cancellation
    is final: always due."""
    from .reminders import overdue_event, reminder_event  # reminders imports this module

    meeting = row.meeting
    if row.kind == Kind.CANCELLED:
        return True
    if row.kind in (Kind.INVITED, Kind.CHANGED):
        return not meeting.is_cancelled
    if row.kind == Kind.MINUTES:
        return not meeting.is_cancelled and meeting.minutes_version >= 1
    if row.kind == Kind.REMINDER:
        return (
            meeting.status == Meeting.Status.SCHEDULED
            and reminder_event(meeting) == row.event_id
            and meeting.starts_at > timezone.now()
        )
    if row.kind == Kind.ACTION_OVERDUE:
        actions = ActionItem.objects.filter(
            meeting=meeting, status=ActionItem.Status.OPEN, assignee_id=row.recipient_id
        )
        return any(overdue_event(a) == row.event_id for a in actions)
    return False


def skip_reason(row: MeetingNotification) -> str | None:
    return delivery.skip_reason(
        row,
        thesis_open=is_open_for(row.meeting.thesis, "meetings"),
        concerned=concerned,
        still_due=still_due,
    )


# --------------------------------------------------------------------------- queueing


def _queue(notification: MeetingNotification) -> None:
    from .tasks import send_meeting_notification

    notification_id = str(notification.pk)  # the broker sees this id only

    def send() -> None:
        transaction.on_commit(lambda: send_meeting_notification.delay(notification_id))

    # M11b: immediate, deferred to a digest, or not wanted (the preference).
    routing.dispatch(notification, "meetings", send)


def notify(
    meeting: Meeting,
    kind: NotificationKind,
    event_id: uuid.UUID,
    recipients: Iterable[User],
    *,
    exclude: User | None = None,
) -> int:
    """Record and queue one notification per distinct recipient who is still an
    active internal member of the meeting's thesis; return how many."""
    count = 0
    members = internal_member_ids(meeting.thesis_id)  # never a former member
    for user in {u.pk: u for u in recipients}.values():
        if user.pk not in members or (exclude is not None and user.pk == exclude.pk):
            continue
        notification = MeetingNotification.objects.create(
            meeting=meeting, kind=kind, event_id=event_id, recipient=user
        )
        _queue(notification)
        count += 1
    return count


def requeue_failed() -> int:
    """Queue again the FAILED notifications below the attempt limit (daily run)."""
    count = 0
    failed = MeetingNotification.objects.filter(
        status=MeetingNotification.Status.FAILED, attempts__lt=MAX_ATTEMPTS
    ).order_by("created_at", "pk")
    for notification in failed:
        with transaction.atomic():
            _queue(notification)
        count += 1
    return count
