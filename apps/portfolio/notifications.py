"""Notifications of the portfolio (M9a): the reminder before the re-registration.

notify() runs inside its own transaction: one row per recipient still CONCERNED,
queued after commit with its id only. A row is unique per (kind, target, window,
recipient): a reminder run twice, or two runs at once, sends nothing twice. The
e-mail is a link only (CLAUDE.md rule 7). Category « Échéances » (M11b,
activity.routing, source "portfolio").

Concerned = an active user, still the active PHD_STUDENT of the thesis (checked at
queue time and again when the e-mail is sent). Still due = the re-registration
date of the thesis is still the one reminded, and not past.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from typing import Any

from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.accounts.models import Membership, Thesis, User
from apps.accounts.roles import Role
from apps.accounts.statuses import is_open_for
from apps.activity import routing
from apps.core import delivery

from .models import NotificationKind, NotificationStatus, PortfolioNotification, PortfolioPolicy

MAX_ATTEMPTS = 3


def is_concerned(user: Any, thesis: Thesis) -> bool:
    if not getattr(user, "is_active", False):
        return False
    return (
        Membership.objects.active()
        .filter(user_id=user.pk, thesis_id=thesis.pk, role=Role.PHD_STUDENT)
        .exists()
    )


# --------------------------------------------------------------------------- send-time check
# Checked by the sending task under the row's lock (apps.core.delivery): the
# state may have changed since the row was queued.


def concerned(row: PortfolioNotification) -> bool:
    return is_concerned(row.recipient, row.thesis)


def still_due(row: PortfolioNotification) -> bool:
    """The date reminded is still the thesis's next re-registration, not past: a
    date changed or cleared meanwhile -> SKIPPED, not sent."""
    if row.kind != NotificationKind.REREGISTRATION:
        return False
    current = (
        PortfolioPolicy.objects.filter(thesis_id=row.thesis_id)
        .values_list("reregistration_date", flat=True)
        .first()
    )
    return current is not None and current == row.due_on and current >= timezone.localdate()


def skip_reason(row: PortfolioNotification) -> str | None:
    return delivery.skip_reason(
        row,
        thesis_open=is_open_for(row.thesis, "portfolio"),
        concerned=concerned,
        still_due=still_due,
    )


# --------------------------------------------------------------------------- queueing


def _queue(row: PortfolioNotification) -> None:
    from .tasks import send_portfolio_notification

    row_id = str(row.pk)  # the broker sees this id only

    def send() -> None:
        transaction.on_commit(lambda: send_portfolio_notification.delay(row_id))

    # M11b: immediate, deferred to a digest, or not wanted (the preference).
    routing.dispatch(row, "portfolio", send)


def notify(
    thesis: Thesis,
    target_id: uuid.UUID,
    recipients: Iterable[User],
    *,
    window: str,
    due_on: Any,
) -> int:
    """Record and queue one reminder per new (target, window, recipient)."""
    count = 0
    for user in {u.pk: u for u in recipients}.values():
        if not is_concerned(user, thesis):
            continue
        try:
            with transaction.atomic():
                row = PortfolioNotification.objects.create(
                    thesis=thesis,
                    kind=NotificationKind.REREGISTRATION,
                    target_id=target_id,
                    window=window,
                    due_on=due_on,
                    recipient=user,
                )
                _queue(row)
        except IntegrityError:  # already recorded (a twin run, or an earlier one)
            continue
        count += 1
    return count


def students_of(thesis: Thesis) -> list[User]:
    ids = (
        Membership.objects.active()
        .filter(thesis=thesis, role=Role.PHD_STUDENT, user__is_active=True)
        .values_list("user_id", flat=True)
    )
    return list(User.objects.filter(pk__in=list(ids)).order_by("pk"))


def requeue_failed() -> int:
    count = 0
    failed = PortfolioNotification.objects.filter(
        status=NotificationStatus.FAILED, attempts__lt=MAX_ATTEMPTS
    ).order_by("created_at", "pk")
    for row in failed:
        with transaction.atomic():
            _queue(row)
        count += 1
    return count
