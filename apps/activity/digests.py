"""Daily run of the activity app (`manage.py activity_digests`, a systemd timer at
07:00 in Paris): overdue objectives, the digests, and FAILED e-mails queued again.

Days are DATES in Paris. A daily digest on day D covers the Informations of D-1;
a weekly one, on a Monday D, those of D-7 .. D-1. M11b: a digest also gathers
every DEFERRED row of the recipient whose category is at its frequency, in every
queue (routing.SOURCES), WHATEVER ITS DATE: a change of frequency loses nothing.
Each row is checked again by its module's skip_reason (SKIPPED otherwise) and
linked to the digest (DIGESTED): never sent alone, never twice. One digest per
(thesis, kind, period, recipient): a run repeated, or two at once, sends once.
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import Any

from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.accounts.models import Thesis
from apps.accounts.statuses import any_open, is_open_for

from .choices import Category, EventKind, Frequency, NotificationKind
from .models import ActivityNotification, Event
from .notifications import MAX_ATTEMPTS, _members, _queue, objective_overdue, unread_in_period
from .preferences import frequency_of
from .routing import SOURCES, deferred

logger = logging.getLogger("apps.core.delivery")


def _due(today: dt.date) -> list[tuple[NotificationKind, Frequency, dt.date]]:
    out = [(NotificationKind.DIGEST_DAILY, Frequency.DAILY, today - dt.timedelta(days=1))]
    if today.weekday() == 0:  # Monday
        out.append((NotificationKind.DIGEST_WEEKLY, Frequency.WEEKLY, today - dt.timedelta(days=7)))
    return out


def _overdue_objectives(thesis: Thesis, today: dt.date) -> int:
    """OBJECTIVE_OVERDUE, once per objective, written by the system (no author)."""
    from apps.objectives.models import Objective

    from . import events

    written = 0
    candidates = Objective.objects.filter(
        thesis=thesis, approved_at__isnull=False, due_date__lt=today
    )
    for objective in candidates:
        if not objective_overdue(objective, today):
            continue
        if Event.objects.filter(kind=EventKind.OBJECTIVE_OVERDUE, object_id=objective.pk).exists():
            continue
        with transaction.atomic():
            events.record(
                kind=EventKind.OBJECTIVE_OVERDUE, obj=objective, object_type="OBJECTIVE",
                thesis=thesis, actor=None,
            )  # fmt: skip
        written += 1
    return written


def _digest(
    thesis: Thesis, user: Any, kind: NotificationKind, start: dt.date, categories: set[Any]
) -> bool:
    """Build one digest of ``user``; True if one was queued."""
    with transaction.atomic():
        gathered = []
        for source in SOURCES.values():
            for row in deferred(source, user, thesis.pk).select_for_update():
                if source.category(row) not in categories:
                    continue
                reason = source.skip_reason(row)
                if reason is not None:
                    source.model().objects.filter(pk=row.pk).update(status="SKIPPED")
                    logger.info("%s skipped (%s, %s)", source.key, row.pk, reason)
                    continue
                gathered.append((source, row))
        includes_info = Category.INFO in categories and unread_in_period(user, thesis, kind, start)
        if not gathered and not includes_info:
            return False
        try:
            with transaction.atomic():  # savepoint: a digest of this period exists
                digest = ActivityNotification.objects.create(
                    thesis=thesis, recipient=user, kind=kind, period_start=start,
                    includes_info=includes_info,
                )  # fmt: skip
        except IntegrityError:
            return False  # the rows wait for the next digest
        for source, row in gathered:
            source.model().objects.filter(pk=row.pk).update(status="DIGESTED", digest=digest)
        _queue(digest)
    return True


def run(today: dt.date | None = None) -> dict[str, int]:
    today = today or timezone.localdate()
    started = timezone.now()  # a row failing in THIS run is retried by the next one
    queued = again = overdue = 0
    # M13a: wherever one module is open (apps/accounts/statuses.py); each row is
    # then checked by its own module (skip_reason), the Informations by theirs.
    for thesis in Thesis.objects.filter(status__in=any_open()):
        if is_open_for(thesis, "objectives"):
            overdue += _overdue_objectives(thesis, today)
        for user in _members(thesis.pk):
            frequencies = {c: frequency_of(user, thesis, c) for c in Category}
            for kind, wanted, start in _due(today):
                categories = {c for c, f in frequencies.items() if f == wanted}
                if categories and _digest(thesis, user, kind, start, categories):
                    queued += 1
    for row in ActivityNotification.objects.filter(
        status=ActivityNotification.Status.FAILED,
        attempts__lt=MAX_ATTEMPTS,
        updated_at__lt=started,
    ):
        ActivityNotification.objects.filter(pk=row.pk).update(
            status=ActivityNotification.Status.PENDING
        )
        with transaction.atomic():
            _queue(row)
        again += 1
    return {"queued": queued, "again": again, "overdue": overdue}
