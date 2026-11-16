"""Celery tasks of the conferences. Arguments are ids only."""

from __future__ import annotations

from typing import Any

from celery import shared_task
from django.conf import settings
from django.template.loader import render_to_string
from django.urls import reverse

from apps.core.delivery import deliver_once

from .models import ConferenceNotification, NotificationKind, StApproval


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


def _message(row: Any) -> tuple[str, str]:
    context = {"url": settings.SITE_URL + _url(row), "kind": row.kind}
    subject = render_to_string("conferences/email/notification_subject.txt", context).strip()
    body = render_to_string("conferences/email/notification_body.txt", context)
    return subject, body


@shared_task(ignore_result=True)  # type: ignore[untyped-decorator]  # Celery is untyped
def send_conference_notification(row_id: str) -> None:
    """E-mail one notification: a link, nothing else (rule 7). Locked row (sent
    once); at send time the thesis must be open, the recipient still concerned and
    the deadline or approval still calling for it (notifications.skip_reason),
    SKIPPED otherwise; no automatic retry; ids only in the logs."""
    from .notifications import MAX_ATTEMPTS, skip_reason

    deliver_once(
        ConferenceNotification,
        row_id,
        label="conferences notification",
        max_attempts=MAX_ATTEMPTS,
        skip_reason=skip_reason,
        message=_message,
        select_related=("thesis", "recipient"),
    )
