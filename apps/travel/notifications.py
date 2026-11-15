"""Notifications of the travel module (the meetings' and conferences' mechanism).

notify() runs INSIDE the caller's transaction, before its audit(): one row per
recipient still CONCERNED (is_concerned() below), queued after commit with its id only.
A row is unique per (kind, target, window, recipient): a reminder run twice, or two
runs at once, sends nothing twice. The e-mail is a link only (CLAUDE.md rule 7).

Concerned = still holding the role the notification is for, checked again when the
e-mail is sent: the student for their reminders and decisions; the payer's person
(tutor for ST, director for the LIRMM) who reads the mission, for an approval; a
supervisor or the student for a rules change. Never a co-supervisor.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from typing import Any

from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.accounts.models import Membership, Thesis, User
from apps.accounts.permissions import can_access_level, roles_of
from apps.accounts.roles import Role
from apps.core import delivery
from apps.core.confidentiality import ConfidentialityLevel

from .choices import NotificationKind, NotificationStatus
from .models import Mission, TravelNotification

MAX_ATTEMPTS = 3
STUDENT_KINDS = frozenset(
    {
        NotificationKind.ORDER_TO_REQUEST,
        NotificationKind.RECEIPTS_MISSING,
        NotificationKind.ORDER_DECIDED,
    }
)
DECIDER_KINDS = frozenset({NotificationKind.ORDER_REQUESTED, NotificationKind.ORDER_PENDING})
DECIDER_ROLE = {"ST": Role.INDUSTRIAL_TUTOR, "LIRMM": Role.DIRECTOR}
RULES_ROLES = frozenset({Role.PHD_STUDENT, Role.DIRECTOR, Role.INDUSTRIAL_TUTOR})


def is_concerned(user: Any, thesis: Thesis, kind: str, mission: Mission | None) -> bool:
    """Whether ``user`` is concerned (queue time, and send time through concerned)."""
    roles = roles_of(user, thesis)
    if kind in STUDENT_KINDS:
        return Role.PHD_STUDENT in roles
    if kind in DECIDER_KINDS:
        if mission is None or mission.payer not in DECIDER_ROLE:
            return False
        return DECIDER_ROLE[mission.payer] in roles and can_access_level(
            user, thesis, ConfidentialityLevel(mission.level)
        )
    if kind == NotificationKind.RULES_CHANGED:
        return bool(roles & RULES_ROLES)
    return False


# --------------------------------------------------------------------------- send-time check
# Checked by the sending task under the row's lock (apps.core.delivery): the
# state may have changed since the row was queued.


def concerned(row: TravelNotification) -> bool:
    return is_concerned(row.recipient, row.thesis, row.kind, row.mission)


def still_due(row: TravelNotification) -> bool:
    """Whether the mission still calls for this e-mail when it is SENT (it may have
    changed since it was queued): a cancelled mission, an approval requested or
    decided meanwhile, a receipt deposited meanwhile -> SKIPPED, not sent."""
    from . import services
    from .choices import MissionStatus, OrderDecision
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


def skip_reason(row: TravelNotification) -> str | None:
    return delivery.skip_reason(
        row, thesis_open=row.thesis.is_open, concerned=concerned, still_due=still_due
    )


# --------------------------------------------------------------------------- queueing


def _queue(row: TravelNotification) -> None:
    from .tasks import send_travel_notification

    row_id = str(row.pk)  # the broker sees this id only
    transaction.on_commit(lambda: send_travel_notification.delay(row_id))


def notify(
    thesis: Thesis,
    kind: NotificationKind,
    target_id: uuid.UUID,
    recipients: Iterable[User],
    *,
    window: str = "",
    mission: Mission | None = None,
    exclude: User | None = None,
) -> int:
    """Record and queue one notification per new (kind, target, window, recipient)."""
    count = 0
    for user in {u.pk: u for u in recipients}.values():
        if exclude is not None and user.pk == exclude.pk:
            continue
        if not is_concerned(user, thesis, kind, mission):
            continue
        try:
            with transaction.atomic():
                row = TravelNotification.objects.create(
                    thesis=thesis,
                    kind=kind,
                    target_id=target_id,
                    window=window,
                    recipient=user,
                    mission=mission,
                )
                _queue(row)
        except IntegrityError:  # already recorded (a twin run, or an earlier one)
            continue
        count += 1
    return count


def members_with_roles(thesis: Thesis, *roles: Role) -> list[User]:
    ids = (
        Membership.objects.active()
        .filter(thesis=thesis, role__in=roles, user__is_active=True)
        .values_list("user_id", flat=True)
    )
    return list(User.objects.filter(pk__in=list(ids)).order_by("pk"))


def requeue_failed() -> int:
    count = 0
    failed = TravelNotification.objects.filter(
        status=NotificationStatus.FAILED, attempts__lt=MAX_ATTEMPTS
    ).order_by("created_at", "pk")
    for row in failed:
        with transaction.atomic():
            _queue(row)
        count += 1
    return count
