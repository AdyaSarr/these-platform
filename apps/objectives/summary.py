"""Weekly summary of the objectives to the supervisors (a link only, CLAUDE.md rule 7).

Run every Monday by `manage.py objectives_weekly_summary` (systemd timer, see
docs/DEPLOYMENT.md); running it again, or twice at the same time, sends nothing
twice (WeeklySummaryLog's unique constraint), as the reminders of M3.

- The week is computed on DATES in Paris: ``week_start`` is the Monday of
  today's date in Paris, and the summarised week the seven days before it
  (Monday 00:00 to Monday 00:00, Paris time: 167 or 169 hours across a change).
- Recipients: active DIRECTOR, CO_SUPERVISOR, INDUSTRIAL_TUTOR,
  INDUSTRIAL_CO_SUPERVISOR of a SETUP or
  ACTIVE thesis (never the student, the CSI member or a guest).
- Sent only if there is something for THIS recipient, on the objectives whose
  level they may read: activity during the week (objective created or changed,
  revision, progress, comment), an overdue objective, or, for a role that
  validates objectives, one to acknowledge.
- The broker and the logs see the WeeklySummaryLog id only; no title, no count.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Iterable

from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone

from apps.accounts.models import Membership, Thesis, User
from apps.accounts.permissions import Action, can, can_access_level
from apps.accounts.roles import Role

from .models import (
    OBJECTIVE_LEVELS,
    Objective,
    ObjectiveComment,
    ObjectiveRevision,
    ProgressUpdate,
    Status,
    WeeklySummaryLog,
)

RECIPIENT_ROLES = (
    Role.DIRECTOR,
    Role.CO_SUPERVISOR,
    Role.INDUSTRIAL_TUTOR,
    Role.INDUSTRIAL_CO_SUPERVISOR,
)
OPEN_THESIS = (Thesis.Status.SETUP, Thesis.Status.ACTIVE)
MAX_ATTEMPTS = 3


def week_start(today: dt.date | None = None) -> dt.date:
    """The Monday of ``today`` (default: today's date in Paris)."""
    if today is None:
        today = timezone.localdate()
    if isinstance(today, dt.datetime) or not isinstance(today, dt.date):
        raise TypeError("week_start works on a date (the date in Paris)")
    return today - dt.timedelta(days=today.weekday())


def window(monday: dt.date) -> tuple[dt.datetime, dt.datetime]:
    """[previous Monday 00:00, this Monday 00:00), Paris time, as aware datetimes."""
    zone = timezone.get_current_timezone()
    start = dt.datetime.combine(monday - dt.timedelta(days=7), dt.time.min, tzinfo=zone)
    end = dt.datetime.combine(monday, dt.time.min, tzinfo=zone)
    return start, end


def _active_objective_ids(thesis: Thesis, start: dt.datetime, end: dt.datetime) -> set[uuid.UUID]:
    """Objectives of ``thesis`` with any activity in [start, end)."""
    in_week = {"created_at__gte": start, "created_at__lt": end}
    ids: set[uuid.UUID] = set(
        Objective.objects.filter(thesis=thesis)
        .filter(Q(**in_week) | Q(updated_at__gte=start, updated_at__lt=end))
        .values_list("pk", flat=True)
    )
    for model in (ObjectiveRevision, ProgressUpdate, ObjectiveComment):
        ids |= set(
            model.objects.filter(thesis=thesis, **in_week).values_list("objective_id", flat=True)
        )
    return ids


def _readable_levels(user: User, thesis: Thesis) -> list[str]:
    return [level.value for level in OBJECTIVE_LEVELS if can_access_level(user, thesis, level)]


def has_news(user: User, thesis: Thesis, today: dt.date, active: set[uuid.UUID]) -> bool:
    """Whether the summary of ``thesis`` has something for ``user``."""
    if not can(user, Action.OBJECTIVE_READ, thesis):
        return False
    readable = Objective.objects.filter(thesis=thesis, level__in=_readable_levels(user, thesis))
    if readable.filter(pk__in=active).exists():
        return True
    overdue = readable.filter(
        approved_at__isnull=False,
        status__in=(Status.PLANNED, Status.IN_PROGRESS),
        due_date__lt=today,
    )
    if overdue.exists():
        return True
    if can(user, Action.VALIDATE_OBJECTIVES, thesis):
        pending = readable.filter(approved_at__isnull=False).filter(
            Q(changed_since_approval=True) | Q(result_declared=True)
        )
        return pending.exists()
    return False


def _recipients(thesis: Thesis, at: dt.datetime) -> list[User]:
    user_ids = (
        Membership.objects.active(at)
        .filter(thesis_id=thesis.pk, role__in=list(RECIPIENT_ROLES), user__is_active=True)
        .values_list("user_id", flat=True)
    )
    return list(User.objects.filter(pk__in=user_ids).order_by("pk"))


def _claim(thesis: Thesis, recipient: User, monday: dt.date) -> WeeklySummaryLog | None:
    """The summary to queue, or None if another run (or an earlier one this week) has it.

    The INSERT is the claim; a FAILED summary below the attempt limit is queued
    again (the task locks the row: two queued copies still send once).
    """
    try:
        with transaction.atomic():
            return WeeklySummaryLog.objects.create(
                thesis=thesis, recipient=recipient, week_start=monday
            )
    except IntegrityError:
        existing = WeeklySummaryLog.objects.get(
            thesis=thesis, recipient=recipient, week_start=monday
        )
    if existing.status == WeeklySummaryLog.Status.FAILED and existing.attempts < MAX_ATTEMPTS:
        return existing
    return None


def _queue(summary: WeeklySummaryLog) -> None:
    from .tasks import send_weekly_summary

    summary_id = str(summary.pk)  # the broker sees this id only
    transaction.on_commit(lambda: send_weekly_summary.delay(summary_id))


def run(today: dt.date | None = None) -> dict[str, int]:
    """Claim and queue this week's summaries; return counts (no title, no address)."""
    today = today or timezone.localdate()  # the date in Paris (TIME_ZONE)
    monday = week_start(today)
    start, end = window(monday)
    at = timezone.now()
    theses: Iterable[Thesis] = Thesis.objects.filter(status__in=OPEN_THESIS).order_by("pk")
    counts = {"theses": 0, "queued": 0}
    for thesis in theses:
        counts["theses"] += 1
        active = _active_objective_ids(thesis, start, end)
        for recipient in _recipients(thesis, at):
            if not has_news(recipient, thesis, today, active):
                continue
            with transaction.atomic():
                summary = _claim(thesis, recipient, monday)
                if summary is not None:
                    _queue(summary)
                    counts["queued"] += 1
    return counts
