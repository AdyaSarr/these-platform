"""Celery tasks of the objectives. Arguments are ids only."""

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

from .models import WeeklySummaryLog

logger = logging.getLogger(__name__)


@shared_task(ignore_result=True)  # type: ignore[untyped-decorator]  # Celery is untyped
def send_weekly_summary(summary_id: str) -> None:
    """E-mail one weekly summary: a link to the objectives, nothing else (rule 7).

    The row is locked: two queued copies send it once. No automatic retry: the
    next run of the week queues a FAILED summary again, up to MAX_ATTEMPTS.
    Logs hold the summary id only, never a title, an address or the SMTP reply.
    """
    from .summary import MAX_ATTEMPTS

    with transaction.atomic():
        summary = (
            WeeklySummaryLog.objects.select_for_update()
            .select_related("recipient")
            .filter(pk=summary_id)
            .first()
        )
        if (
            summary is None
            or summary.status == WeeklySummaryLog.Status.SENT
            or summary.attempts >= MAX_ATTEMPTS
            or not summary.recipient.is_active
        ):
            return
        url = settings.SITE_URL + reverse(
            "objectives:dashboard", kwargs={"thesis_id": summary.thesis_id}
        )
        subject = render_to_string("objectives/email/summary_subject.txt").strip()
        body = render_to_string("objectives/email/summary_body.txt", {"url": url})
        summary.attempts += 1
        try:
            send_mail(subject, body, None, [summary.recipient.email])
        except (smtplib.SMTPException, OSError, BadHeaderError):
            # The exception text may echo the message or the server: id only.
            logger.warning("weekly summary e-mail failed (summary %s)", summary.pk, exc_info=False)
            summary.status = WeeklySummaryLog.Status.FAILED
            summary.save(update_fields=["status", "attempts", "updated_at"])
            return
        summary.status = WeeklySummaryLog.Status.SENT
        summary.sent_at = timezone.now()
        summary.save(update_fields=["status", "attempts", "sent_at", "updated_at"])
