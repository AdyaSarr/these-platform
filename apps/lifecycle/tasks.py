"""Celery task of the lifecycle e-mails (M13a). Arguments are ids only."""

from __future__ import annotations

from typing import Any

from celery import shared_task
from django.conf import settings
from django.template.loader import render_to_string
from django.urls import reverse

from apps.core.delivery import deliver_once

from .models import LifecycleNotification


def _message(row: Any) -> tuple[str, str]:
    url = settings.SITE_URL + reverse("lifecycle:page", kwargs={"thesis_id": row.thesis_id})
    context = {"url": url, "kind": row.kind}
    subject = render_to_string("lifecycle/email/notification_subject.txt", context).strip()
    body = render_to_string("lifecycle/email/notification_body.txt", context)
    return subject, body


@shared_task(ignore_result=True)  # type: ignore[untyped-decorator]  # Celery is untyped
def send_lifecycle_notification(notification_id: str) -> None:
    """E-mail one lifecycle notification: a link to the « Fin de thèse » page only
    (rule 7). Sent at once, never deferred to a digest: a digest is built for an
    open thesis only, and a closure request expires in 14 days (M13a)."""
    from .notifications import MAX_ATTEMPTS, skip_reason

    deliver_once(
        LifecycleNotification,
        notification_id,
        label="lifecycle notification",
        max_attempts=MAX_ATTEMPTS,
        skip_reason=skip_reason,
        message=_message,
        select_related=("thesis", "recipient", "closure_request"),
        lock_of=("self",),
    )
