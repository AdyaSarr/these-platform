"""The feed, the bell, reading and retention (M11a).

Filter first, cut afterwards (the rule of « À venir »): every list, count and page
is computed on readers.readable_events(), the exact SQL form of reads().
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Any

from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.db.models import QuerySet
from django.utils import timezone

from apps.accounts.models import Thesis

from .choices import MODULE_OF, MODULES
from .labels import name_of, objects_of, target_of
from .models import ActivityNotification, Event, EventRead
from .preferences import frequency_of, set_preference
from .readers import readable_events

__all__ = [
    "Bell",
    "Row",
    "bell",
    "digest_page",
    "feed",
    "frequency_of",
    "mark_all_read",
    "open_event",
    "purge",
    "set_preference",
]

BELL_SIZE = 8
PAGE_SIZE = 20
RETENTION_MONTHS = 13


@dataclass(frozen=True)
class Row:
    event: Event
    name: str
    unread: bool


@dataclass(frozen=True)
class Bell:
    unread: int
    latest: list[Row]


@dataclass(frozen=True)
class Page:
    rows: list[Row]
    number: int
    has_previous: bool
    has_next: bool
    module: str


def _ordered(user: Any, thesis: Thesis) -> QuerySet[Event]:
    return readable_events(user, thesis).order_by("-created_at", "-pk")


def _rows(user: Any, events: list[Event]) -> list[Row]:
    objects = objects_of(events)
    read = set(
        EventRead.objects.filter(event__in=events, user_id=user.pk).values_list(
            "event_id", flat=True
        )
    )
    rows = []
    for event in events:
        obj = objects.get(event.object_id)
        if obj is not None:  # the prefilter guarantees it; never a name of nothing
            rows.append(Row(event, name_of(event, obj), event.pk not in read))
    return rows


def _unread(user: Any, events: QuerySet[Event]) -> QuerySet[Event]:
    return events.exclude(reads__user_id=user.pk)


def bell(user: Any, thesis: Thesis) -> Bell:
    """The number of unread elements and the BELL_SIZE latest, after the filter."""
    events = _ordered(user, thesis)
    unread = _unread(user, events).count()
    return Bell(unread=unread, latest=_rows(user, list(events[:BELL_SIZE])))


def feed(user: Any, thesis: Thesis, *, module: str = "", page: int = 1) -> Page:
    """One page of the feed, optionally of one module (MODULES)."""
    events = _ordered(user, thesis)
    if module:
        if module not in MODULES:
            module = ""
        else:
            types = [t for t, m in MODULE_OF.items() if m == module]
            events = events.filter(object_type__in=types)
    page = max(1, page)
    start = (page - 1) * PAGE_SIZE
    chunk = list(events[start : start + PAGE_SIZE + 1])
    return Page(
        rows=_rows(user, chunk[:PAGE_SIZE]),
        number=page,
        has_previous=page > 1,
        has_next=len(chunk) > PAGE_SIZE,
        module=module,
    )


def _deny(user: Any, thesis: Thesis, operation: str) -> PermissionDenied:
    """Audited, in its own transaction (kept although 403 follows); one reason for
    an unknown, foreign or unreadable event: they look alike."""
    from apps.vault.audit import AuditAction, audit

    with transaction.atomic():
        audit(
            AuditAction.ACTIVITY_ACCESS_DENIED,
            actor=user,
            thesis=thesis,
            operation=operation,
            reason="unknown_event",
        )
    return PermissionDenied()


def open_event(user: Any, thesis: Thesis, event_id: Any) -> str:
    """Mark the event read and return the page of its element; the only effect."""
    event = readable_events(user, thesis).filter(pk=event_id).first()
    if event is None:
        raise _deny(user, thesis, "open")
    obj = objects_of([event]).get(event.object_id)
    if obj is None:
        raise _deny(user, thesis, "open")
    EventRead.objects.get_or_create(event=event, user_id=user.pk)
    return target_of(event, obj)


@dataclass(frozen=True)
class DigestPage:
    digest: ActivityNotification
    items: list[tuple[str, str]]  # (name, page) of the gathered rows still holding
    rows: list[Row]  # the Informations of its period still readable


def digest_page(user: Any, thesis: Thesis, digest_id: Any) -> DigestPage:
    """The « Résumé » of one digest e-mail, recomputed now; its recipient only
    (anyone else, or an unknown id: the same refusal)."""
    from .notifications import digest_rows, period
    from .routing import describe

    digest = (
        ActivityNotification.objects.filter(pk=digest_id, thesis_id=thesis.pk, recipient_id=user.pk)
        .exclude(kind="EVENT")
        .first()
        if user.is_authenticated
        else None
    )
    if digest is None or digest.period_start is None:
        raise _deny(user, thesis, "digest")
    items = [
        describe(source, row)
        for source, row in digest_rows(digest)
        if source.skip_reason(row) is None
    ]
    rows: list[Row] = []
    if digest.includes_info:
        first, end = period(digest.kind, digest.period_start)
        events = _ordered(user, thesis).filter(created_at__gte=first, created_at__lt=end)
        rows = _rows(user, list(events[:200]))
    return DigestPage(digest=digest, items=items, rows=rows)


def mark_all_read(user: Any, thesis: Thesis) -> int:
    """Every event ``user`` reads on ``thesis`` is marked read."""
    unread = _unread(user, _ordered(user, thesis)).values_list("pk", flat=True)
    rows = [EventRead(event_id=pk, user_id=user.pk) for pk in unread]
    EventRead.objects.bulk_create(rows, ignore_conflicts=True)
    return len(rows)


def _months_back(instant: dt.datetime, months: int) -> dt.datetime:
    import calendar

    index = instant.year * 12 + (instant.month - 1) - months
    year, month = divmod(index, 12)
    day = min(instant.day, calendar.monthrange(year, month + 1)[1])
    return instant.replace(year=year, month=month + 1, day=day)


def purge(now: dt.datetime | None = None) -> int:
    """Delete what is older than RETENTION_MONTHS: events (with their reads and
    e-mails) and digests."""
    cutoff = _months_back(now or timezone.now(), RETENTION_MONTHS)
    with transaction.atomic():
        digests = ActivityNotification.objects.filter(event__isnull=True, created_at__lt=cutoff)
        removed_digests, _ = digests.delete()
        removed, _ = Event.objects.filter(created_at__lt=cutoff).delete()
        # M11b: the history of the former Monday summary (objectives) goes too.
        from apps.objectives.models import WeeklySummaryLog

        old_summaries, _ = WeeklySummaryLog.objects.filter(created_at__lt=cutoff).delete()
    return removed + removed_digests + old_summaries
