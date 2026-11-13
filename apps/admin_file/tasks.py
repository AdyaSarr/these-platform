"""Celery tasks of the administrative file. Arguments are ids only."""

from __future__ import annotations

import logging
import smtplib

from celery import shared_task
from django.conf import settings
from django.core.mail import BadHeaderError, send_mail
from django.db import transaction
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import timezone

from .models import ReminderLog

logger = logging.getLogger(__name__)


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
        subject = render_to_string("admin_file/email/reminder_subject.txt").strip()
        body = render_to_string("admin_file/email/reminder_body.txt", {"url": url})
        reminder.attempts += 1
        try:
            send_mail(subject, body, None, [reminder.recipient.email])
        except (smtplib.SMTPException, OSError, BadHeaderError):
            # The exception text may echo the message or the server: type and id only.
            logger.warning("reminder e-mail failed (reminder %s)", reminder.pk, exc_info=False)
            reminder.status = ReminderLog.Status.FAILED
            reminder.save(update_fields=["status", "attempts", "updated_at"])
            return
        reminder.status = ReminderLog.Status.SENT
        reminder.sent_at = timezone.now()
        reminder.save(update_fields=["status", "attempts", "sent_at", "updated_at"])
