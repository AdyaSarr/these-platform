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
from apps.accounts.roles import Role

from .models import ItemNotification, RequiredItem
from .reminders import MAX_ATTEMPTS, active_members

Kind = ItemNotification.Kind


def _queue(notification: ItemNotification) -> None:
    from .tasks import send_item_notification

    notification_id = str(notification.pk)  # the broker sees this id only
    transaction.on_commit(lambda: send_item_notification.delay(notification_id))


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
