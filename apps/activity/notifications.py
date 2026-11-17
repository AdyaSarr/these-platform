"""E-mails of the activity app (M11a): who is queued, and the send-time checks.

Queued at record time (inside the service's transaction, ids only through the
broker): « informations » to the readers whose preference is immediate; three
e-mails that ask for an action, whatever the preference (a piece submitted -> its
validators; a piece refused -> the student; an action assigned -> its assignee).
Digests are queued by digests.run(). Nothing for an event an existing e-mail
already covers (choices.EMAIL_OF).

At send time, under the row's lock (core.delivery.deliver_once): the thesis is
open, the recipient still CONCERNED and the e-mail still DUE, else SKIPPED.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from django.db import transaction
from django.utils import timezone

from apps.accounts.models import Membership, User
from apps.accounts.permissions import can_access_level, roles_of
from apps.accounts.roles import Role
from apps.core import delivery
from apps.core.confidentiality import ConfidentialityLevel

from .choices import EMAIL_OF, Category, EventKind, Frequency, NotificationKind
from .models import ActivityNotification, Event, EventRead
from .preferences import frequency_of
from .readers import load, readable_events, reads

MAX_ATTEMPTS = 3
K = EventKind
DIGEST_FREQUENCY = {
    NotificationKind.DIGEST_DAILY: Frequency.DAILY,
    NotificationKind.DIGEST_WEEKLY: Frequency.WEEKLY,
}


def _members(thesis_id: Any) -> list[User]:
    ids = (
        Membership.objects.active()
        .filter(thesis_id=thesis_id, user__is_active=True)
        .values_list("user_id", flat=True)
    )
    return list(User.objects.filter(pk__in=set(ids)).order_by("pk"))


# --------------------------------------------------------------------------- the three actions


def _validator(user: Any, item: Any) -> bool:
    """An active validator of ``item`` who reads its section, not its submitter."""
    from .readers import RULES

    held = roles_of(user, item.thesis)
    return (
        any(Role(r) in held for r in item.validator_roles)
        and item.submitted_by_id != user.pk
        and RULES["ITEM"](user, item)
    )


def _assignee(user: Any, action: Any) -> bool:
    from apps.meetings.members import is_internal_member

    meeting = action.meeting
    return (
        action.assignee_id == user.pk
        and is_internal_member(user, meeting.thesis_id)
        and can_access_level(user, meeting.thesis, ConfidentialityLevel(meeting.level))
    )


def _action_recipients(event: Event, obj: Any) -> list[tuple[User, int | None]]:
    if event.kind == K.ITEM_SUBMITTED:
        return [(u, obj.cycle) for u in _members(event.thesis_id) if _validator(u, obj)]
    if event.kind == K.ITEM_REJECTED:
        students = [
            u for u in _members(event.thesis_id) if Role.PHD_STUDENT in roles_of(u, obj.thesis)
        ]
        return [(u, obj.cycle) for u in students]
    if event.kind == K.ACTION_ASSIGNED:
        assignee = obj.assignee
        return [(assignee, None)] if assignee.is_active and _assignee(assignee, obj) else []
    return []


# --------------------------------------------------------------------------- queueing


def _queue(row: ActivityNotification) -> None:
    from .tasks import send_activity_notification

    row_id = str(row.pk)  # the broker sees this id only
    transaction.on_commit(lambda: send_activity_notification.delay(row_id))


def queue_for_event(event: Event, obj: Any) -> int:
    """Record and queue the immediate e-mails of ``event``; return how many."""
    email = EMAIL_OF[event.kind]
    if email == "NONE":
        return 0
    if email == "ACTION":
        recipients = _action_recipients(event, obj)
    else:
        recipients = [
            (u, None)
            for u in _members(event.thesis_id)
            if u.pk != event.actor_id
            and frequency_of(u, event.thesis, Category.INFO) == Frequency.IMMEDIATE
            and reads(u, event)
        ]
    for user, cycle in recipients:
        row = ActivityNotification.objects.create(
            thesis_id=event.thesis_id,
            recipient=user,
            kind=NotificationKind.EVENT,
            event=event,
            cycle=cycle,
        )
        _queue(row)
    return len(recipients)


# --------------------------------------------------------------------------- send time


def period(kind: str, start: dt.date) -> tuple[dt.datetime, dt.datetime]:
    """[first instant, end instant) of a digest's period, in Paris dates."""
    days = 1 if kind == NotificationKind.DIGEST_DAILY else 7
    tz = timezone.get_current_timezone()
    first = dt.datetime.combine(start, dt.time(0), tzinfo=tz)
    return first, dt.datetime.combine(start + dt.timedelta(days=days), dt.time(0), tzinfo=tz)


def unread_in_period(user: Any, thesis: Any, kind: str, start: dt.date) -> bool:
    first, end = period(kind, start)
    return (
        readable_events(user, thesis)
        .filter(created_at__gte=first, created_at__lt=end)
        .exclude(reads__user_id=user.pk)
        .exists()
    )


def concerned(row: ActivityNotification) -> bool:
    user = row.recipient
    if not user.is_active or not roles_of(user, row.thesis):
        return False
    if row.kind != NotificationKind.EVENT:
        wanted = DIGEST_FREQUENCY[NotificationKind(row.kind)]
        return frequency_of(user, row.thesis, Category.INFO) == wanted
    event = row.event
    if event is None:
        return False
    if EMAIL_OF[event.kind] == "INFO":
        return frequency_of(user, row.thesis, Category.INFO) == Frequency.IMMEDIATE and reads(
            user, event
        )
    obj = load(event)
    if obj is None:
        return False
    if event.kind == K.ITEM_SUBMITTED:
        return _validator(user, obj)
    if event.kind == K.ITEM_REJECTED:
        return Role.PHD_STUDENT in roles_of(user, row.thesis)
    if event.kind == K.ACTION_ASSIGNED:
        return _assignee(user, obj)
    return False


def still_due(row: ActivityNotification) -> bool:
    from apps.admin_file.models import ItemApproval, ItemStatus
    from apps.meetings.models import ActionItem

    if row.kind != NotificationKind.EVENT:
        if row.period_start is None:
            return False
        return unread_in_period(row.recipient, row.thesis, row.kind, row.period_start)
    event = row.event
    if event is None:
        return False
    if EMAIL_OF[event.kind] == "INFO":
        return not EventRead.objects.filter(event=event, user_id=row.recipient_id).exists()
    obj = load(event)
    if obj is None:
        return False
    if event.kind == K.ITEM_SUBMITTED:
        return bool(
            obj.status == ItemStatus.SUBMITTED
            and obj.cycle == row.cycle
            and not ItemApproval.objects.filter(
                item=obj, cycle=obj.cycle, user_id=row.recipient_id
            ).exists()
        )
    if event.kind == K.ITEM_REJECTED:
        return bool(obj.status == ItemStatus.REJECTED and obj.cycle == row.cycle)
    if event.kind == K.ACTION_ASSIGNED:
        return bool(obj.status == ActionItem.Status.OPEN)
    return False


def skip_reason(row: ActivityNotification) -> str | None:
    return delivery.skip_reason(
        row, thesis_open=row.thesis.is_open, concerned=concerned, still_due=still_due
    )
