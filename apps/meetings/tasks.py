"""Celery tasks of the meetings. Arguments are ids only."""

from __future__ import annotations

from typing import Any

from celery import shared_task
from django.conf import settings
from django.template.loader import render_to_string
from django.urls import reverse

from apps.core.delivery import deliver_once

from .models import MeetingNotification


def _action_anchor(row: Any) -> str:
    """ACTION_OVERDUE leads to the action itself (M11a): its anchor on the page."""
    from .models import ActionItem, NotificationKind
    from .reminders import overdue_event

    if row.kind != NotificationKind.ACTION_OVERDUE:
        return ""
    for action in ActionItem.objects.filter(meeting_id=row.meeting_id):
        if overdue_event(action) == row.event_id:
            return f"#action-{action.pk}"
    return ""


def _message(row: Any) -> tuple[str, str]:
    url = (
        settings.SITE_URL
        + reverse(
            "meetings:detail",
            kwargs={"thesis_id": row.meeting.thesis_id, "meeting_id": row.meeting_id},
        )
        + _action_anchor(row)
    )
    context = {"url": url, "kind": row.kind}
    subject = render_to_string("meetings/email/notification_subject.txt", context).strip()
    body = render_to_string("meetings/email/notification_body.txt", context)
    return subject, body


@shared_task(ignore_result=True)  # type: ignore[untyped-decorator]  # Celery is untyped
def send_meeting_notification(notification_id: str) -> None:
    """E-mail one meeting notification: a link to the meeting, nothing else (rule 7).

    The row is locked: two queued copies send it once. At send time the thesis
    must be open, the recipient still concerned and the meeting still calling for
    it (notifications.skip_reason), SKIPPED otherwise. No automatic retry: the
    daily run queues a FAILED one again, up to notifications.MAX_ATTEMPTS. Logs
    hold the notification id only, never a title, an address or the SMTP reply.
    """
    from .notifications import MAX_ATTEMPTS, skip_reason

    deliver_once(
        MeetingNotification,
        notification_id,
        label="meetings notification",
        max_attempts=MAX_ATTEMPTS,
        skip_reason=skip_reason,
        message=_message,
        select_related=("meeting__thesis", "recipient"),
    )
