"""Celery tasks of the objectives. Arguments are ids only."""

from __future__ import annotations

from celery import shared_task
from django.conf import settings
from django.template.loader import render_to_string
from django.urls import reverse

from apps.core.delivery import deliver_once

from .models import WeeklySummaryLog


def _message(summary: WeeklySummaryLog) -> tuple[str, str]:
    url = settings.SITE_URL + reverse(
        "objectives:dashboard", kwargs={"thesis_id": summary.thesis_id}
    )
    subject = render_to_string("objectives/email/summary_subject.txt").strip()
    body = render_to_string("objectives/email/summary_body.txt", {"url": url})
    return subject, body


@shared_task(ignore_result=True)  # type: ignore[untyped-decorator]  # Celery is untyped
def send_weekly_summary(summary_id: str) -> None:
    """E-mail one weekly summary: a link to the objectives, nothing else (rule 7).

    Through core.delivery.deliver_once (M11a): the row is locked, two queued copies
    send it once; at send time the thesis must be open, the recipient still
    concerned and the summary still due (notifications.skip_reason), SKIPPED
    otherwise. No automatic retry: the next run of the week queues a FAILED
    summary again, up to MAX_ATTEMPTS. Logs hold the summary id only.
    """
    from .notifications import skip_reason
    from .summary import MAX_ATTEMPTS

    deliver_once(
        WeeklySummaryLog,
        summary_id,
        label="weekly summary",
        max_attempts=MAX_ATTEMPTS,
        skip_reason=skip_reason,
        message=_message,
        select_related=("thesis", "recipient"),
    )
