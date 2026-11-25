"""Celery tasks of the portfolio (M9a). Arguments are ids only."""

from __future__ import annotations

from typing import Any

from celery import shared_task
from django.conf import settings
from django.template.loader import render_to_string
from django.urls import reverse

from apps.activity import routing
from apps.core.delivery import deliver_once

from .models import PortfolioNotification


def _url(row: Any) -> str:
    """The page the e-mail links to; ids only in the URL."""
    return reverse("portfolio:trainings", kwargs={"thesis_id": row.thesis_id})


def _message(row: Any) -> tuple[str, str]:
    context = {"url": settings.SITE_URL + _url(row), "kind": row.kind, "window": row.window}
    subject = render_to_string("portfolio/email/notification_subject.txt", context).strip()
    body = render_to_string("portfolio/email/notification_body.txt", context)
    return subject, body


@shared_task(ignore_result=True)  # type: ignore[untyped-decorator]  # Celery is untyped
def send_portfolio_notification(row_id: str) -> None:
    """E-mail one reminder: a link, nothing else (rule 7). Locked row (sent once,
    even by two workers at once); at send time the thesis must be open, the
    recipient still the student and the date still the one reminded
    (notifications.skip_reason), SKIPPED otherwise; no automatic retry; ids only
    in the logs."""
    from .notifications import MAX_ATTEMPTS, skip_reason

    deliver_once(
        PortfolioNotification,
        row_id,
        label="portfolio notification",
        max_attempts=MAX_ATTEMPTS,
        skip_reason=skip_reason,
        message=_message,
        select_related=("recipient", "thesis"),
        route=routing.route_at_send("portfolio"),
    )
