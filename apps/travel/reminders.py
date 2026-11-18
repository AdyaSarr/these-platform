"""Daily reminders of the travel module (run by `manage.py travel_reminders`, a
systemd timer). Days are DATES in Paris, never hours.

- mission approval to request (the student): a mission paid by ST or the LIRMM,
  planned or refused, departing in the future: EARLY when N < days <= N + 7, DUE when
  0 <= days <= N (N = TravelPolicy.order_lead_days);
- approval awaiting a decision (the payer's person): J-7 (7..2 days before the
  departure), J-1 (1..0);
- receipts or actual amounts missing (the student): a mission DONE with a line
  without actual amount, or without receipt unless flat-rate: AFTER_GRACE from
  receipt_grace_days to 13 days after the return, AFTER_14 from 14 days.
Each once per (event, window, recipient); a moved departure or return is a new
event. Open theses only; the e-mail is a link (CLAUDE.md rule 7).
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass

from django.utils import timezone

from apps.accounts.models import Thesis
from apps.accounts.roles import Role
from apps.accounts.statuses import is_open_for, open_statuses
from apps.core import timezones

from . import services
from .choices import MissionStatus, NotificationKind, OrderDecision, Window
from .models import ORDER_PAYERS, Mission, MissionOrder
from .notifications import members_with_roles, notify, requeue_failed

OPEN_THESIS = open_statuses("travel_receipts")  # apps/accounts/statuses.py
# Fixed namespace of the reminders' event ids (uuid5): never change it.
EVENT_NAMESPACE = uuid.UUID("6f3b2a1c-4d5e-4f6a-8b9c-0d1e2f3a4b5c")
TO_REQUEST = (MissionStatus.PLANNED, MissionStatus.ORDER_REFUSED)


@dataclass(frozen=True)
class RunResult:
    to_request: int
    pending: int
    receipts: int
    requeued: int


def event(*parts: object) -> uuid.UUID:
    return uuid.uuid5(EVENT_NAMESPACE, ":".join(str(p) for p in parts))


def order_window(days: int, *, lead_days: int) -> str | None:
    if lead_days < days <= lead_days + 7:
        return Window.EARLY
    if 0 <= days <= lead_days:
        return Window.DUE
    return None


def pending_window(days: int) -> str | None:
    if 2 <= days <= 7:
        return Window.J7
    if 0 <= days <= 1:
        return Window.J1
    return None


def receipt_window(days: int, *, grace_days: int) -> str | None:
    if days >= 14:
        return Window.AFTER_14
    if grace_days <= days <= 13:
        return Window.AFTER_GRACE
    return None


def run(now: dt.datetime | None = None) -> RunResult:
    if now is not None and (not isinstance(now, dt.datetime) or now.tzinfo is None):
        raise ValueError("run() needs an aware datetime")
    now = now or timezone.now()
    requeued = requeue_failed()  # earlier failures first: a new row is tried once per run
    today = timezones.local_date(now, "Europe/Paris")
    to_request = pending = receipts = 0
    for thesis in Thesis.objects.filter(status__in=OPEN_THESIS).order_by("pk"):
        policy = services.policy_of(thesis)
        students = members_with_roles(thesis, Role.PHD_STUDENT)
        deciders = members_with_roles(thesis, Role.INDUSTRIAL_TUTOR, Role.DIRECTOR)
        # M13a: the approvals follow M7 (frozen after the defence); the receipts of
        # a mission begun before the defence stay due (apps/accounts/statuses.py).
        orders_open = is_open_for(thesis, "travel")
        for mission in (
            Mission.objects.filter(
                thesis=thesis, status__in=TO_REQUEST, payer__in=ORDER_PAYERS, start_date__gte=today
            ).order_by("start_date", "pk")
            if orders_open
            else []
        ):
            window = order_window(
                (mission.start_date - today).days, lead_days=policy.order_lead_days
            )
            if window is not None:
                to_request += notify(
                    thesis,
                    NotificationKind.ORDER_TO_REQUEST,
                    event("to_request", mission.pk, mission.start_date.isoformat()),
                    students,
                    window=window,
                    mission=mission,
                )
        for order in (
            MissionOrder.objects.filter(
                thesis=thesis, decision=OrderDecision.PENDING, mission__start_date__gte=today
            ).select_related("mission")
            if orders_open
            else []
        ):
            window = pending_window((order.mission.start_date - today).days)
            if window is not None:
                pending += notify(
                    thesis,
                    NotificationKind.ORDER_PENDING,
                    event("pending", order.pk),
                    deciders,
                    window=window,
                    mission=order.mission,
                )
        for mission in Mission.objects.filter(thesis=thesis, status=MissionStatus.DONE).order_by(
            "end_date", "pk"
        ):
            window = receipt_window(
                (today - mission.end_date).days, grace_days=policy.receipt_grace_days
            )
            if (
                window is not None
                and services.receipts_open(thesis, mission)
                and services.expenses_incomplete(mission)
            ):
                receipts += notify(
                    thesis,
                    NotificationKind.RECEIPTS_MISSING,
                    event("receipts", mission.pk, mission.end_date.isoformat()),
                    students,
                    window=window,
                    mission=mission,
                )
    return RunResult(to_request, pending, receipts, requeued)
