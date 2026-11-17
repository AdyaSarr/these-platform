"""Celery task of the activity app. Arguments are ids only."""

from __future__ import annotations

from typing import Any

from celery import shared_task
from django.conf import settings
from django.template.loader import render_to_string
from django.urls import reverse

from apps.activity import routing
from apps.core.delivery import deliver_once

from .choices import EMAIL_OF, EventKind, NotificationKind
from .models import ActivityNotification


def link_of(row: Any) -> str:
    site: str = settings.SITE_URL
    """The one link of the e-mail: the element itself, or the feed for a digest."""
    thesis_id = row.thesis_id
    if row.kind != NotificationKind.EVENT:  # a digest: its « Résumé » page
        return site + reverse(
            "activity:digest", kwargs={"thesis_id": thesis_id, "digest_id": row.pk}
        )
    event = row.event
    if event.kind in (EventKind.ITEM_SUBMITTED, EventKind.ITEM_REJECTED):
        return site + reverse(
            "admin_file:item", kwargs={"thesis_id": thesis_id, "item_id": event.object_id}
        )
    if event.kind == EventKind.ACTION_ASSIGNED:
        from apps.meetings.models import ActionItem

        action = ActionItem.objects.get(pk=event.object_id)
        detail = reverse(
            "meetings:detail", kwargs={"thesis_id": thesis_id, "meeting_id": action.meeting_id}
        )
        return f"{site}{detail}#action-{action.pk}"
    return site + reverse("activity:open", kwargs={"thesis_id": thesis_id, "event_id": event.pk})


def _message(row: Any) -> tuple[str, str]:
    category = "DIGEST" if row.kind != NotificationKind.EVENT else EMAIL_OF[row.event.kind]
    context = {"url": link_of(row), "category": category}
    subject = render_to_string("activity/email/subject.txt", context).strip()
    body = render_to_string("activity/email/body.txt", context)
    return subject, body


@shared_task(ignore_result=True)  # type: ignore[untyped-decorator]  # Celery is untyped
def send_activity_notification(notification_id: str) -> None:
    """E-mail one activity notification: a link only (CLAUDE.md rule 7).

    The row is locked: two queued copies send it once. At send time the thesis
    must be open, the recipient still concerned and the e-mail still due
    (notifications.skip_reason), SKIPPED otherwise. No automatic retry: the daily
    `activity_digests` run queues a FAILED one again, up to MAX_ATTEMPTS. Logs hold
    the row id only.
    """
    from .notifications import MAX_ATTEMPTS, skip_reason

    deliver_once(
        ActivityNotification,
        notification_id,
        label="activity notification",
        max_attempts=MAX_ATTEMPTS,
        skip_reason=skip_reason,
        message=_message,
        select_related=("thesis", "recipient"),
        route=routing.route_at_send("activity"),
    )
