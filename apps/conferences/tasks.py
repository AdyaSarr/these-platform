"""Celery tasks of the conferences. Arguments are ids only."""

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

from apps.meetings.members import is_internal_member

from .models import (
    ConferenceNotification,
    NotificationKind,
    NotificationStatus,
    StApproval,
)

logger = logging.getLogger(__name__)


def _url(row: ConferenceNotification) -> str:
    """The page the e-mail links to; ids only in the URL."""
    thesis = {"thesis_id": row.thesis_id}
    if row.kind == NotificationKind.DEADLINE and row.link_id:
        return reverse("conferences:edition", kwargs={**thesis, "edition_id": row.link_id})
    if (
        row.kind in (NotificationKind.REQUEST_DUE, NotificationKind.APPROVAL_PENDING)
        and row.link_id
    ):
        return reverse("conferences:submission", kwargs={**thesis, "submission_id": row.link_id})
    if row.kind in (NotificationKind.APPROVAL_REQUESTED, NotificationKind.APPROVAL_DECIDED):
        approval = StApproval.objects.filter(pk=row.target_id).first()
        if approval is not None:
            return reverse(
                "conferences:submission",
                kwargs={**thesis, "submission_id": approval.submission_id},
            )
    if row.kind == NotificationKind.POLICY_CHANGED:
        return reverse("conferences:policy", kwargs=thesis)
    return reverse("conferences:deadlines", kwargs=thesis)


@shared_task(ignore_result=True)  # type: ignore[untyped-decorator]  # Celery is untyped
def send_conference_notification(row_id: str) -> None:
    """E-mail one notification: a link, nothing else (rule 7). Locked row (sent
    once); the recipient must still be an active internal member (SKIPPED
    otherwise); no automatic retry; ids only in the logs."""
    from .notifications import MAX_ATTEMPTS

    with transaction.atomic():
        row = (
            ConferenceNotification.objects.select_for_update()
            .select_related("recipient")
            .filter(pk=row_id)
            .first()
        )
        if (
            row is None
            or row.status in (NotificationStatus.SENT, NotificationStatus.SKIPPED)
            or row.attempts >= MAX_ATTEMPTS
        ):
            return
        if not is_internal_member(row.recipient, row.thesis_id):
            row.status = NotificationStatus.SKIPPED
            row.save(update_fields=["status", "updated_at"])
            return
        context = {"url": settings.SITE_URL + _url(row), "kind": row.kind}
        subject = render_to_string("conferences/email/notification_subject.txt", context).strip()
        body = render_to_string("conferences/email/notification_body.txt", context)
        row.attempts += 1
        try:
            send_mail(subject, body, None, [row.recipient.email])
        except (smtplib.SMTPException, OSError, BadHeaderError):
            logger.warning("conference notification e-mail failed (%s)", row.pk, exc_info=False)
            row.status = NotificationStatus.FAILED
            row.save(update_fields=["status", "attempts", "updated_at"])
            return
        row.status = NotificationStatus.SENT
        row.sent_at = timezone.now()
        row.save(update_fields=["status", "attempts", "sent_at", "updated_at"])
