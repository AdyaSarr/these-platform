"""Notifications of item events (reopening, due dates): the reminders' mechanism.

notify() runs INSIDE the caller's transaction (thesis and item locked), before
its audit(): one ItemNotification per recipient, queued after commit with its id
only (the broker never sees an address, a label or a reason). The e-mail is a
link to the item, nothing else (CLAUDE.md rule 7). A FAILED notification is
queued again by the daily `admin_file_reminders` run, up to MAX_ATTEMPTS.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable

from django.db import transaction
from django.utils import timezone

from apps.accounts.models import User
from apps.accounts.permissions import can, roles_of
from apps.accounts.roles import Role
from apps.activity import routing
from apps.core import delivery

from .models import (
    SECTION_READ_ACTION,
    DueDateProposal,
    ItemApproval,
    ItemNotification,
    ItemReopening,
    ItemStatus,
    ReminderLog,
    RequiredItem,
    Section,
)
from .reminders import MAX_ATTEMPTS, active_members, recipients

Kind = ItemNotification.Kind
STUDENT_DECISIONS = frozenset({Kind.DUE_ACCEPTED, Kind.DUE_REFUSED})

# --------------------------------------------------------------------------- send-time check
# Checked by the sending tasks under the row's lock (apps.core.delivery): the
# state may have changed since the row was queued.


def _proposal(row: ItemNotification) -> DueDateProposal | None:
    return DueDateProposal.objects.filter(pk=row.event_id, item_id=row.item_id).first()


def concerned(row: ReminderLog | ItemNotification) -> bool:
    """The recipient still holds, on an active account and membership, the role
    the e-mail is for, and still reads the item's section."""
    user, item = row.recipient, row.item
    roles = roles_of(user, item.thesis)
    if not roles or not can(user, SECTION_READ_ACTION[Section(item.section)], item):
        return False
    validators = {Role(r) for r in item.validator_roles}
    if isinstance(row, ReminderLog):  # the student, or a validator, by the item's state
        return Role.PHD_STUDENT in roles or bool(roles & validators)
    if row.kind == Kind.REOPENED:  # the author of a cancelled approval, in its role
        reopening = ItemReopening.objects.filter(pk=row.event_id, item=item).first()
        if reopening is None:
            return False
        role = (
            ItemApproval.objects.filter(item=item, cycle=reopening.cycle, user_id=user.pk)
            .values_list("role", flat=True)
            .first()
        )
        return role is not None and Role(role) in roles
    if row.kind == Kind.DUE_PROPOSED:  # a validator, never the proposal's author
        proposal = _proposal(row)
        return (
            proposal is not None and proposal.proposed_by_id != user.pk and bool(roles & validators)
        )
    if row.kind in STUDENT_DECISIONS:  # the proposal's author, still the student
        proposal = _proposal(row)
        return (
            proposal is not None
            and proposal.proposed_by_id == user.pk
            and Role.PHD_STUDENT in roles
        )
    if row.kind == Kind.DUE_CHANGED:
        return Role.PHD_STUDENT in roles
    return False


def still_due(row: ReminderLog | ItemNotification) -> bool:
    """The item still calls for this e-mail. A reminder: same due date, and the
    recipient is still one the item's state calls (the student of an awaited item,
    a validator whose role has not approved, never the submitter). An event that
    happened (a decision, a direct change) stays due; a reopening until the item
    is validated again; a proposal while it waits for a decision."""
    item = row.item
    if isinstance(row, ReminderLog):
        if item.due_date is None or item.due_date != row.due_date:
            return False
        return any(u.pk == row.recipient_id for u in recipients(item, timezone.now()))
    if row.kind == Kind.REOPENED:
        return item.status != ItemStatus.VALIDATED
    if row.kind == Kind.DUE_PROPOSED:
        proposal = _proposal(row)
        return proposal is not None and proposal.status == DueDateProposal.Status.PENDING
    return row.kind in STUDENT_DECISIONS or row.kind == Kind.DUE_CHANGED


def skip_reason(row: ReminderLog | ItemNotification) -> str | None:
    return delivery.skip_reason(
        row, thesis_open=row.item.thesis.is_open, concerned=concerned, still_due=still_due
    )


# --------------------------------------------------------------------------- queueing


def _queue(notification: ItemNotification) -> None:
    from .tasks import send_item_notification

    notification_id = str(notification.pk)  # the broker sees this id only

    def send() -> None:
        transaction.on_commit(lambda: send_item_notification.delay(notification_id))

    # M11b: immediate, deferred to a digest, or not wanted (the preference).
    routing.dispatch(notification, "admin_file.notification", send)


def notify(
    item: RequiredItem, kind: ItemNotification.Kind, event_id: uuid.UUID, recipients: Iterable[User]
) -> int:
    """Record and queue one notification per distinct recipient; return how many."""
    count = 0
    for user in {u.pk: u for u in recipients}.values():
        notification = ItemNotification.objects.create(
            item=item, kind=kind, event_id=event_id, recipient=user
        )
        _queue(notification)
        count += 1
    return count


def members(
    item: RequiredItem, roles: Iterable[Role], *, exclude: User | None = None
) -> list[User]:
    """Active members of the item's thesis holding one of ``roles``, minus ``exclude``."""
    found = active_members(item, list(roles), timezone.now())
    return [u for u in found if exclude is None or u.pk != exclude.pk]


def requeue_failed() -> int:
    """Queue again the FAILED notifications below the attempt limit (daily run)."""
    count = 0
    failed = ItemNotification.objects.filter(
        status=ItemNotification.Status.FAILED, attempts__lt=MAX_ATTEMPTS
    ).order_by("created_at", "pk")
    for notification in failed:
        with transaction.atomic():
            _queue(notification)
        count += 1
    return count
