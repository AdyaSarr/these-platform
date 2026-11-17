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
from functools import partial
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


def _pending_acknowledgement(objective: Any) -> bool:
    """An approved objective changed, or whose result was declared (M4)."""
    return bool(
        objective.approved_at is not None
        and (objective.changed_since_approval or objective.result_declared)
    )


def category_for(event: Event, user: Any, obj: Any = None) -> str:
    """The category of ``event`` for ``user`` (docs/modules/M11-activite.md, M11b)."""
    from apps.accounts.permissions import Action, can

    if EMAIL_OF[event.kind] == "ACTION":
        return Category.ACTION
    if event.kind in (K.OBJECTIVE_CHANGED, K.PROGRESS_REPORTED):
        obj = obj if obj is not None else load(event)
        if (
            obj is not None
            and can(user, Action.VALIDATE_OBJECTIVES, obj)
            and _pending_acknowledgement(obj)
        ):
            return Category.ACTION  # to acknowledge (director)
    if event.kind == K.OBJECTIVE_OVERDUE and Role.PHD_STUDENT in roles_of(user, event.thesis):
        return Category.DEADLINE
    return Category.INFO


def queue_for_event(event: Event, obj: Any) -> int:
    """Record the e-mails of ``event`` and route them (routing.dispatch): sent,
    deferred to a digest, or not wanted; return how many rows."""
    from .routing import dispatch

    email = EMAIL_OF[event.kind]
    if email == "NONE":
        return 0
    recipients: list[tuple[User, int | None, str]]
    if email == "ACTION":
        recipients = [(u, c, Category.ACTION) for u, c in _action_recipients(event, obj)]
    else:
        recipients = []
        for u in _members(event.thesis_id):
            if u.pk == event.actor_id or not reads(u, event):
                continue
            category = category_for(event, u, obj)
            frequency = frequency_of(u, event.thesis, category)
            if category == Category.INFO and frequency != Frequency.IMMEDIATE:
                continue  # an Information in a digest is the feed's (digests.run)
            recipients.append((u, None, category))
    for user, cycle, category in recipients:
        row = ActivityNotification.objects.create(
            thesis_id=event.thesis_id,
            recipient=user,
            kind=NotificationKind.EVENT,
            event=event,
            cycle=cycle,
            category=category,
        )
        dispatch(row, "activity", partial(_queue, row))
    return len(recipients)


# --------------------------------------------------------------------------- send time


def period(kind: str, start: dt.date) -> tuple[dt.datetime, dt.datetime]:
    """[first instant, end instant) of a digest's period, in Paris dates."""
    if type(start) is not dt.date:  # a datetime is a date too: refused, never truncated
        raise TypeError("a digest's period starts on a date")
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
        return True  # a digest: its content is checked by still_due
    event = row.event
    if event is None:
        return False
    obj = load(event)
    if obj is None:
        return False
    if row.category == Category.ACTION and event.kind in (K.OBJECTIVE_CHANGED, K.PROGRESS_REPORTED):
        return reads(user, event) and category_for(event, user, obj) == Category.ACTION
    if row.category == Category.DEADLINE and event.kind == K.OBJECTIVE_OVERDUE:
        return reads(user, event) and Role.PHD_STUDENT in roles_of(user, row.thesis)
    if EMAIL_OF[event.kind] == "INFO":
        return reads(user, event)
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
        return digest_still_due(row)
    event = row.event
    if event is None:
        return False
    obj = load(event)
    if obj is None:
        return False
    if row.category == Category.ACTION and event.kind in (K.OBJECTIVE_CHANGED, K.PROGRESS_REPORTED):
        return _pending_acknowledgement(obj)
    if row.category == Category.DEADLINE and event.kind == K.OBJECTIVE_OVERDUE:
        return objective_overdue(obj, timezone.localdate())
    if EMAIL_OF[event.kind] == "INFO":
        return not EventRead.objects.filter(event=event, user_id=row.recipient_id).exists()
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


def objective_overdue(objective: Any, today: dt.date) -> bool:
    """Approved, neither achieved nor dropped, and past its due date (M4)."""
    from apps.objectives.models import Status

    return bool(
        objective.approved_at is not None
        and objective.status in (Status.PLANNED, Status.IN_PROGRESS)
        and objective.due_date < today
    )


def digest_rows(digest: ActivityNotification) -> list[tuple[Any, Any]]:
    """(source, row) of every row a digest gathered."""
    from .routing import SOURCES

    out = []
    for source in SOURCES.values():
        rows = source.model().objects.filter(digest_id=digest.pk, recipient_id=digest.recipient_id)
        out += [(source, row) for row in rows]
    return out


def digest_still_due(digest: ActivityNotification) -> bool:
    """One of its rows still holds (its module's checks), or an Information of
    its period is still readable and unread."""
    if any(source.skip_reason(row) is None for source, row in digest_rows(digest)):
        return True
    if not digest.includes_info or digest.period_start is None:
        return False
    return unread_in_period(digest.recipient, digest.thesis, digest.kind, digest.period_start)


def skip_reason(row: ActivityNotification) -> str | None:
    return delivery.skip_reason(
        row, thesis_open=row.thesis.is_open, concerned=concerned, still_due=still_due
    )
