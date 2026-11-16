"""Celery tasks of the administrative file. Arguments are ids only."""

from __future__ import annotations

from typing import Any

from celery import shared_task
from django.conf import settings
from django.template.loader import render_to_string
from django.urls import reverse

from apps.core.delivery import deliver_once

from .models import ItemNotification, ReminderLog


def _message(template: str, url: str) -> tuple[str, str]:
    subject = render_to_string(f"admin_file/email/{template}_subject.txt").strip()
    body = render_to_string(f"admin_file/email/{template}_body.txt", {"url": url})
    return subject, body


def _reminder_message(row: Any) -> tuple[str, str]:
    url = reverse("admin_file:file", kwargs={"thesis_id": row.item.thesis_id})
    return _message("reminder", settings.SITE_URL + url)


def _notification_message(row: Any) -> tuple[str, str]:
    url = reverse(
        "admin_file:item", kwargs={"thesis_id": row.item.thesis_id, "item_id": row.item_id}
    )
    return _message("notification", settings.SITE_URL + url)


@shared_task(ignore_result=True)  # type: ignore[untyped-decorator]  # Celery is untyped
def send_reminder_email(reminder_id: str) -> None:
    """E-mail one reminder: a link to the administrative file, nothing else (rule 7).

    The row is locked: two queued copies of one reminder (two concurrent runs
    retrying a FAILED one) send it once. The thesis must be open, the recipient
    still concerned and the item still awaited (notifications.skip_reason),
    SKIPPED otherwise. No automatic retry: the next daily run queues a FAILED
    reminder again, up to reminders.MAX_ATTEMPTS. Logs hold the reminder id only,
    never the label, the code, the address or the SMTP reply.
    """
    from .notifications import skip_reason
    from .reminders import MAX_ATTEMPTS

    deliver_once(
        ReminderLog,
        reminder_id,
        label="admin_file reminder",
        max_attempts=MAX_ATTEMPTS,
        skip_reason=skip_reason,
        message=_reminder_message,
        select_related=("item__thesis", "recipient"),
    )


@shared_task(ignore_result=True)  # type: ignore[untyped-decorator]  # Celery is untyped
def send_item_notification(notification_id: str) -> None:
    """E-mail one item notification: a link to the item, nothing else (rule 7).

    Same rules as send_reminder_email: locked row (sent once), checked again at
    send time (SKIPPED otherwise), no automatic retry (the daily run queues a
    FAILED one again), ids only in the logs.
    """
    from .notifications import skip_reason
    from .reminders import MAX_ATTEMPTS

    deliver_once(
        ItemNotification,
        notification_id,
        label="admin_file notification",
        max_attempts=MAX_ATTEMPTS,
        skip_reason=skip_reason,
        message=_notification_message,
        select_related=("item__thesis", "recipient"),
    )
