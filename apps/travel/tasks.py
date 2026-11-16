"""Celery tasks of the travel module. Arguments are ids only."""

from __future__ import annotations

from typing import Any

from celery import shared_task
from django.conf import settings
from django.template.loader import render_to_string
from django.urls import reverse

from apps.core.delivery import deliver_once

from .models import TravelNotification


def _url(row: TravelNotification) -> str:
    """The page the e-mail links to; ids only in the URL."""
    thesis = {"thesis_id": row.thesis_id}
    if row.mission_id is not None:
        return reverse("travel:mission", kwargs={**thesis, "mission_id": row.mission_id})
    return reverse("travel:settings", kwargs=thesis)


def _message(row: Any) -> tuple[str, str]:
    context = {"url": settings.SITE_URL + _url(row), "kind": row.kind}
    subject = render_to_string("travel/email/notification_subject.txt", context).strip()
    body = render_to_string("travel/email/notification_body.txt", context)
    return subject, body


@shared_task(ignore_result=True)  # type: ignore[untyped-decorator]  # Celery is untyped
def send_travel_notification(row_id: str) -> None:
    """E-mail one notification: a link, nothing else (rule 7). Locked row (sent
    once, even by two workers at once); at send time the thesis must be open, the
    recipient still concerned and the mission still calling for it
    (notifications.skip_reason), SKIPPED otherwise; no automatic retry; ids only
    in the logs."""
    from .notifications import MAX_ATTEMPTS, skip_reason

    deliver_once(
        TravelNotification,
        row_id,
        label="travel notification",
        max_attempts=MAX_ATTEMPTS,
        skip_reason=skip_reason,
        message=_message,
        select_related=("recipient", "thesis", "mission"),
        lock_of=("self",),  # the mission is a nullable (outer) join
    )
