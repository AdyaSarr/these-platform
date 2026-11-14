"""Celery tasks of the meetings. Arguments are ids only."""

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

from .models import MeetingNotification

logger = logging.getLogger(__name__)


@shared_task(ignore_result=True)  # type: ignore[untyped-decorator]  # Celery is untyped
def send_meeting_notification(notification_id: str) -> None:
    """E-mail one meeting notification: a link to the meeting, nothing else (rule 7).

    The row is locked: two queued copies send it once. No automatic retry: the
    daily run queues a FAILED one again, up to notifications.MAX_ATTEMPTS. Logs
    hold the notification id only, never a title, an address or the SMTP reply.
    """
    from .notifications import MAX_ATTEMPTS

    with transaction.atomic():
        row = (
            MeetingNotification.objects.select_for_update()
            .select_related("meeting", "recipient")
            .filter(pk=notification_id)
            .first()
        )
        if (
            row is None
            or row.status == MeetingNotification.Status.SENT
            or row.attempts >= MAX_ATTEMPTS
            or not row.recipient.is_active
        ):
            return
        url = settings.SITE_URL + reverse(
            "meetings:detail",
            kwargs={"thesis_id": row.meeting.thesis_id, "meeting_id": row.meeting_id},
        )
        context = {"url": url, "kind": row.kind}
        subject = render_to_string("meetings/email/notification_subject.txt", context).strip()
        body = render_to_string("meetings/email/notification_body.txt", context)
        row.attempts += 1
        try:
            send_mail(subject, body, None, [row.recipient.email])
        except (smtplib.SMTPException, OSError, BadHeaderError):
            logger.warning("meeting notification e-mail failed (%s)", row.pk, exc_info=False)
            row.status = MeetingNotification.Status.FAILED
            row.save(update_fields=["status", "attempts", "updated_at"])
            return
        row.status = MeetingNotification.Status.SENT
        row.sent_at = timezone.now()
        row.save(update_fields=["status", "attempts", "sent_at", "updated_at"])
