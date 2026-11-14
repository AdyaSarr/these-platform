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

from apps.accounts.models import User

from .models import Meeting, MeetingNotification, NotificationKind

Kind = MeetingNotification.Kind
MAX_ATTEMPTS = 3


def _queue(notification: MeetingNotification) -> None:
    from .tasks import send_meeting_notification

    notification_id = str(notification.pk)  # the broker sees this id only
    transaction.on_commit(lambda: send_meeting_notification.delay(notification_id))


def notify(
    meeting: Meeting,
    kind: NotificationKind,
    event_id: uuid.UUID,
    recipients: Iterable[User],
    *,
    exclude: User | None = None,
) -> int:
    """Record and queue one notification per distinct active recipient; return how many."""
    count = 0
    for user in {u.pk: u for u in recipients}.values():
        if not user.is_active or (exclude is not None and user.pk == exclude.pk):
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
