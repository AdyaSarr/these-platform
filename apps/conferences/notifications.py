"""Notifications of the conferences: the meetings' mechanism (M5a).

notify() runs INSIDE the caller's transaction, before its audit(): one row per
recipient who is still an active internal member, queued after commit with its
id only. A row is unique per (kind, target, window, recipient): a reminder run
twice, or two runs at once, sends nothing twice. The e-mail is a link only
(CLAUDE.md rule 7).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable

from django.db import IntegrityError, transaction

from apps.accounts.models import Membership, Thesis, User
from apps.accounts.roles import Role
from apps.meetings.members import internal_member_ids

from .models import ConferenceNotification, NotificationKind, NotificationStatus

MAX_ATTEMPTS = 3


def _queue(row: ConferenceNotification) -> None:
    from .tasks import send_conference_notification

    row_id = str(row.pk)  # the broker sees this id only
    transaction.on_commit(lambda: send_conference_notification.delay(row_id))


def notify(
    thesis: Thesis,
    kind: NotificationKind,
    target_id: uuid.UUID,
    recipients: Iterable[User],
    *,
    window: str = "",
    exclude: User | None = None,
    link_id: uuid.UUID | None = None,
) -> int:
    """Record and queue one notification per new (kind, target, window, recipient)."""
    members = internal_member_ids(thesis.pk)
    count = 0
    for user in {u.pk: u for u in recipients}.values():
        if user.pk not in members or (exclude is not None and user.pk == exclude.pk):
            continue
        try:
            with transaction.atomic():
                row = ConferenceNotification.objects.create(
                    thesis=thesis, kind=kind, target_id=target_id, window=window,
                    recipient=user, link_id=link_id,
                )  # fmt: skip
                _queue(row)
        except IntegrityError:  # already recorded (a twin run, or an earlier one)
            continue
        count += 1
    return count


def members_with_role(thesis: Thesis, role: Role) -> list[User]:
    ids = (
        Membership.objects.active()
        .filter(thesis=thesis, role=role, user__is_active=True)
        .values_list("user_id", flat=True)
    )
    return list(User.objects.filter(pk__in=list(ids)).order_by("pk"))


def requeue_failed() -> int:
    count = 0
    failed = ConferenceNotification.objects.filter(
        status=NotificationStatus.FAILED, attempts__lt=MAX_ATTEMPTS
    ).order_by("created_at", "pk")
    for row in failed:
        with transaction.atomic():
            _queue(row)
        count += 1
    return count
