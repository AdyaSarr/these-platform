"""Reminders of the administrative file: J-14, J-3 and J+1 (overdue).

Run once a day by `manage.py admin_file_reminders` (systemd timer, see
docs/DEPLOYMENT.md); running it again, or twice at the same time, sends nothing
twice (ReminderLog's unique constraint).

- Windows are computed on DATES in Paris (timezone.localdate()), never on hours:
  a run at 23:30 UTC already counts as the next day, and the summer and winter
  time changes move nothing. Only the window of the day is sent: a missed J-14
  is not sent late inside J-3.
- Recipients, by state: TODO or REJECTED -> the PhD student; SUBMITTED -> the
  validators who have not approved the cycle (never its submitter); VALIDATED ->
  nobody. Active members of an open thesis only.
- The e-mail is a link to the platform only (CLAUDE.md rule 7); the broker and
  the logs see the ReminderLog id only.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable

from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone

from apps.accounts.models import Membership, User
from apps.accounts.roles import Role
from apps.accounts.statuses import open_statuses
from apps.activity import routing

from .models import ItemApproval, ItemStatus, ReminderLog, RequiredItem

Kind = ReminderLog.Kind
MAX_ATTEMPTS = 3
OPEN_THESIS = open_statuses("admin_file")  # apps/accounts/statuses.py


def reminder_kind(today: dt.date, due: dt.date) -> ReminderLog.Kind | None:
    """The window ``today`` falls in, in whole days (dates, never datetimes)."""
    if isinstance(today, dt.datetime) or isinstance(due, dt.datetime):
        raise TypeError("reminder windows are computed on dates only")
    offset = (today - due).days
    if offset >= 1:
        return Kind.OVERDUE
    if offset >= -3:
        return Kind.J3
    if offset >= -14:
        return Kind.J14
    return None


def active_members(item: RequiredItem, roles: Iterable[Role], at: dt.datetime) -> list[User]:
    """Active users holding one of ``roles`` on the item's thesis at ``at``."""
    user_ids = (
        Membership.objects.active(at)
        .filter(thesis_id=item.thesis_id, role__in=list(roles), user__is_active=True)
        .values_list("user_id", flat=True)
    )
    return list(User.objects.filter(pk__in=user_ids).order_by("pk"))


def recipients(item: RequiredItem, at: dt.datetime) -> list[User]:
    """Who the item's state calls a reminder for: at queue time, and again at send
    time (notifications.still_due)."""
    status = ItemStatus(item.status)
    if status in (ItemStatus.TODO, ItemStatus.REJECTED):
        return active_members(item, [Role.PHD_STUDENT], at)
    if status != ItemStatus.SUBMITTED:
        return []
    approved = ItemApproval.objects.filter(item=item, cycle=item.cycle)
    done_roles = set(approved.values_list("role", flat=True))
    done_users = set(approved.values_list("user_id", flat=True))
    pending = [Role(r) for r in item.validator_roles if r not in done_roles]
    return [
        u
        for u in active_members(item, pending, at)
        if u.pk not in done_users and u.pk != item.submitted_by_id
    ]


def _claim(item: RequiredItem, kind: ReminderLog.Kind, recipient: User) -> ReminderLog | None:
    """The reminder to queue, or None if another run (or an earlier day) has it.

    The INSERT is the claim: of two concurrent runs, the second one waits on the
    unique index, then finds the row (savepoint) and queues nothing. A FAILED
    reminder below the attempt limit is queued again (the task locks the row,
    so two queued copies still send once).
    """
    due = item.due_date
    if due is None:
        return None
    try:
        with transaction.atomic():
            return ReminderLog.objects.create(
                item=item, kind=kind, due_date=due, recipient=recipient
            )
    except IntegrityError:
        existing = ReminderLog.objects.get(item=item, kind=kind, due_date=due, recipient=recipient)
    if existing.status == ReminderLog.Status.FAILED and existing.attempts < MAX_ATTEMPTS:
        return existing
    return None


def _queue(reminder: ReminderLog) -> None:
    from .tasks import send_reminder_email

    reminder_id = str(reminder.pk)  # the broker sees this id only

    def send() -> None:
        transaction.on_commit(lambda: send_reminder_email.delay(reminder_id))

    # M11b: immediate, deferred to a digest, or not wanted (the preference).
    routing.dispatch(reminder, "admin_file.reminder", send)


def run(today: dt.date | None = None) -> dict[str, int]:
    """Claim and queue today's reminders; return counts (no label, no address)."""
    today = today or timezone.localdate()  # the date in Paris (TIME_ZONE)
    at = timezone.now()
    items = (
        RequiredItem.objects.filter(thesis__status__in=OPEN_THESIS, due_date__isnull=False)
        .filter(~Q(status=ItemStatus.VALIDATED))
        .order_by("thesis_id", "due_date", "pk")
    )
    counts = {"items": 0, "queued": 0, "notifications": 0}
    for item in items:
        due = item.due_date
        kind = reminder_kind(today, due) if due is not None else None
        if kind is None:
            continue
        counts["items"] += 1
        for recipient in recipients(item, at):
            with transaction.atomic():
                reminder = _claim(item, kind, recipient)
                if reminder is not None:
                    _queue(reminder)
                    counts["queued"] += 1
    from .notifications import requeue_failed  # notifications imports this module

    counts["notifications"] = requeue_failed()
    return counts
