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


def still_due(row: TravelNotification) -> bool:
    """Whether the mission still calls for this e-mail when it is SENT (it may have
    changed since it was queued): a cancelled mission, an approval requested or
    decided meanwhile, a receipt deposited meanwhile -> SKIPPED, not sent."""
    from . import services
    from .choices import MissionStatus, NotificationKind, OrderDecision
    from .models import ORDER_PAYERS, MissionOrder

    if row.kind == NotificationKind.RULES_CHANGED:
        return True
    mission = row.mission
    if mission is None or mission.status == MissionStatus.CANCELLED:
        return False
    if row.kind == NotificationKind.ORDER_TO_REQUEST:
        return (
            mission.status in (MissionStatus.PLANNED, MissionStatus.ORDER_REFUSED)
            and mission.payer in ORDER_PAYERS
            and mission.start_date >= timezone.localdate()
        )
    if row.kind == NotificationKind.ORDER_REQUESTED:  # target = the order
        return MissionOrder.objects.filter(
            pk=row.target_id, decision=OrderDecision.PENDING
        ).exists()
    if row.kind == NotificationKind.ORDER_PENDING:
        return MissionOrder.objects.filter(mission=mission, decision=OrderDecision.PENDING).exists()
    if row.kind == NotificationKind.ORDER_DECIDED:
        return True  # the decision happened; only a cancelled mission makes it moot
    if row.kind == NotificationKind.RECEIPTS_MISSING:
        return mission.status == MissionStatus.DONE and services.expenses_incomplete(mission)
    return False


def _url(row: TravelNotification) -> str:
    """The page the e-mail links to; ids only in the URL."""
    thesis = {"thesis_id": row.thesis_id}
    if row.mission_id is not None:
        return reverse("travel:mission", kwargs={**thesis, "mission_id": row.mission_id})
    return reverse("travel:settings", kwargs=thesis)


@shared_task(ignore_result=True)  # type: ignore[untyped-decorator]  # Celery is untyped
def send_travel_notification(row_id: str) -> None:
    """E-mail one notification: a link, nothing else (rule 7). Locked row (sent
    once, even by two workers at once); the recipient must still be concerned and the
    mission must still call for it (still_due), SKIPPED otherwise; no automatic
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
        if not concerned(row.recipient, row.thesis, row.kind, row.mission) or not still_due(row):
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
