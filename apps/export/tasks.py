"""Celery task of the export e-mails (M13b). Arguments are ids only."""

from __future__ import annotations

from typing import Any

from celery import shared_task
from django.conf import settings
from django.template.loader import render_to_string
from django.urls import reverse

from apps.core.delivery import deliver_once

from .models import ExportNotification


def _message(row: Any) -> tuple[str, str]:
    url = settings.SITE_URL + reverse("export:page", kwargs={"thesis_id": row.thesis_id})
    when = row.export.started_at if row.export is not None else row.created_at
    context = {"url": url, "kind": row.kind, "when": when}
    subject = render_to_string("export/email/notification_subject.txt", context).strip()
    body = render_to_string("export/email/notification_body.txt", context)
    return subject, body


@shared_task(ignore_result=True)  # type: ignore[untyped-decorator]  # Celery is untyped
def send_export_notification(notification_id: str) -> None:
    """E-mail one export notification: a link to the export page and, for an
    export, its date (Paris time). Sent at once, never deferred (M13b)."""
    from .notifications import MAX_ATTEMPTS, skip_reason

    deliver_once(
        ExportNotification,
        notification_id,
        label="export notification",
        max_attempts=MAX_ATTEMPTS,
        skip_reason=skip_reason,
        message=_message,
        select_related=("thesis", "recipient", "export", "consent"),
        lock_of=("self",),
    )
