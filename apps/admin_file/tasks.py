"""Celery tasks of the administrative file. Arguments are ids only."""

from __future__ import annotations

import logging
import smtplib
from typing import Any

from celery import shared_task
from django.conf import settings
from django.core.mail import BadHeaderError, send_mail
from django.db import transaction
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import timezone

from .models import ItemNotification, ReminderLog

logger = logging.getLogger(__name__)


def _send_link(row: Any, *, url: str, template: str, label: str) -> None:
    """E-mail ``url`` to ``row.recipient``, nothing else (rule 7); update the row.

    ``row`` is a locked ReminderLog or ItemNotification (status, attempts,
    sent_at). Logs hold the row id only, never the label, the address or the
    SMTP reply.
    """
    subject = render_to_string(f"admin_file/email/{template}_subject.txt").strip()
    body = render_to_string(f"admin_file/email/{template}_body.txt", {"url": url})
    row.attempts += 1
    try:
        send_mail(subject, body, None, [row.recipient.email])
    except (smtplib.SMTPException, OSError, BadHeaderError):
        # The exception text may echo the message or the server: type and id only.
        logger.warning("%s e-mail failed (%s %s)", label, label, row.pk, exc_info=False)
        row.status = row.Status.FAILED
        row.save(update_fields=["status", "attempts", "updated_at"])
        return
    row.status = row.Status.SENT
    row.sent_at = timezone.now()
    row.save(update_fields=["status", "attempts", "sent_at", "updated_at"])


@shared_task(ignore_result=True)  # type: ignore[untyped-decorator]  # Celery is untyped
def send_reminder_email(reminder_id: str) -> None:
    """E-mail one reminder: a link to the administrative file, nothing else (rule 7).

    The row is locked: two queued copies of one reminder (two concurrent runs
    retrying a FAILED one) send it once. No automatic retry: the next daily run
    queues a FAILED reminder again, up to reminders.MAX_ATTEMPTS. Logs hold the
    reminder id only, never the label, the code, the address or the SMTP reply.
    """
    from .reminders import MAX_ATTEMPTS

    with transaction.atomic():
        reminder = (
            ReminderLog.objects.select_for_update()
            .select_related("item", "recipient")
            .filter(pk=reminder_id)
            .first()
        )
        if (
            reminder is None
            or reminder.status == ReminderLog.Status.SENT
            or reminder.attempts >= MAX_ATTEMPTS
            or not reminder.recipient.is_active
        ):
            return
        url = settings.SITE_URL + reverse(
            "admin_file:file", kwargs={"thesis_id": reminder.item.thesis_id}
        )
        _send_link(reminder, url=url, template="reminder", label="reminder")


@shared_task(ignore_result=True)  # type: ignore[untyped-decorator]  # Celery is untyped
def send_item_notification(notification_id: str) -> None:
    """E-mail one item notification: a link to the item, nothing else (rule 7).

    Same rules as send_reminder_email: locked row (sent once), no automatic
    retry (the daily run queues a FAILED one again), ids only in the logs.
    """
    from .reminders import MAX_ATTEMPTS

    with transaction.atomic():
        notification = (
            ItemNotification.objects.select_for_update()
            .select_related("item", "recipient")
            .filter(pk=notification_id)
            .first()
        )
        if (
            notification is None
            or notification.status == ItemNotification.Status.SENT
            or notification.attempts >= MAX_ATTEMPTS
            or not notification.recipient.is_active
        ):
            return
        url = settings.SITE_URL + reverse(
            "admin_file:item",
            kwargs={"thesis_id": notification.item.thesis_id, "item_id": notification.item_id},
        )
        _send_link(notification, url=url, template="notification", label="notification")
