"""Celery tasks of the travel module. Arguments are ids only."""

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

from .choices import NotificationStatus
from .models import TravelNotification

logger = logging.getLogger(__name__)


def _url(row: TravelNotification) -> str:
    """The page the e-mail links to; ids only in the URL."""
    thesis = {"thesis_id": row.thesis_id}
    if row.mission_id is not None:
        return reverse("travel:mission", kwargs={**thesis, "mission_id": row.mission_id})
    return reverse("travel:settings", kwargs=thesis)


@shared_task(ignore_result=True)  # type: ignore[untyped-decorator]  # Celery is untyped
def send_travel_notification(row_id: str) -> None:
    """E-mail one notification: a link, nothing else (rule 7). Locked row (sent
    once); the recipient must still be concerned (SKIPPED otherwise); no automatic
    retry; ids only in the logs."""
    from .notifications import MAX_ATTEMPTS, concerned

    with transaction.atomic():
        row = (
            # Lock the notification row only: the mission is a nullable (outer) join.
            TravelNotification.objects.select_for_update(of=("self",))
            .select_related("recipient", "thesis", "mission")
            .filter(pk=row_id)
            .first()
        )
        if (
            row is None
            or row.status in (NotificationStatus.SENT, NotificationStatus.SKIPPED)
            or row.attempts >= MAX_ATTEMPTS
        ):
            return
        if not concerned(row.recipient, row.thesis, row.kind, row.mission):
            row.status = NotificationStatus.SKIPPED
            row.save(update_fields=["status", "updated_at"])
            return
        context = {"url": settings.SITE_URL + _url(row), "kind": row.kind}
        subject = render_to_string("travel/email/notification_subject.txt", context).strip()
        body = render_to_string("travel/email/notification_body.txt", context)
        row.attempts += 1
        try:
            send_mail(subject, body, None, [row.recipient.email])
        except (smtplib.SMTPException, OSError, BadHeaderError):
            logger.warning("travel notification e-mail failed (%s)", row.pk, exc_info=False)
            row.status = NotificationStatus.FAILED
            row.save(update_fields=["status", "attempts", "updated_at"])
            return
        row.status = NotificationStatus.SENT
        row.sent_at = timezone.now()
        row.save(update_fields=["status", "attempts", "sent_at", "updated_at"])
