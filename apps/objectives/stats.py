"""Statistics of one year's objectives: pure functions, exact arithmetic (Fraction).

Only APPROVED objectives count (a draft is waiting for the director); DROPPED
ones are counted apart and left out of every denominator (acceptance
criterion 3: dropped, not deleted, and not a failure either). An empty
denominator gives None ("—" on screen), never 0. Rounding happens only for
display (as_percent).

- attainment = sum(w * s) / sum(w), s = 1 (ACHIEVED), 1/2 (PARTIAL), 0 otherwise
- progress   = weighted_progress(rows, day): sum(w * p) / (100 * sum(w)), p the
  progress of each objective's last report dated on or before ``day`` (100 for
  ACHIEVED). ONE function for the card (day = today, or the last day of a
  finished year) and the monthly chart (day = each month end, up to the same
  day): rows carry no progress of their own.
- overdue    = due before today (a date in Paris) and still PLANNED or IN_PROGRESS

No text here: titles are never needed to count.
"""

from __future__ import annotations

import datetime as dt
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field
from fractions import Fraction

from .models import Status

SCORE = {Status.ACHIEVED.value: Fraction(1), Status.PARTIAL.value: Fraction(1, 2)}
OPEN = (Status.PLANNED.value, Status.IN_PROGRESS.value)


@dataclass(frozen=True)
class Update:
    date: dt.date
    progress: int
    status: str


@dataclass(frozen=True)
class Row:
    """What the statistics need of one objective (built by services.stats_for)."""

    category: str
    weight: int
    status: str
    due_date: dt.date
    approved: bool
    created_on: dt.date  # date in Paris
    updates: tuple[Update, ...] = ()  # in the order they were recorded


@dataclass(frozen=True)
class Stats:
    attainment: Fraction | None
    progress: Fraction | None
    counted: int  # approved, not dropped
    weight_total: int
    dropped: int
    dropped_weight: int
    drafts: int  # waiting for approval (not dropped)
    overdue: int
    by_status: dict[str, int] = field(default_factory=dict)  # approved, dropped included
    by_category: dict[str, dict[str, int]] = field(default_factory=dict)


def _date(value: dt.date, name: str) -> dt.date:
    if isinstance(value, dt.datetime) or not isinstance(value, dt.date):
        raise TypeError(f"{name} must be a datetime.date (the date in Paris)")
    return value


def _points(status: str, progress: int) -> int:
    return 100 if status == Status.ACHIEVED else progress


def _ratio(numerator: Fraction, denominator: int) -> Fraction | None:
    return numerator / denominator if denominator else None


def compute_stats(
    rows: Iterable[Row], today: dt.date, *, progress_day: dt.date | None = None
) -> Stats:
    """Statistics on ``today``; the progress is measured on ``progress_day`` (default
    today): the last day of a finished year, as its chart's last point."""
    _date(today, "today")
    rows = list(rows)
    approved = [r for r in rows if r.approved]
    active = [r for r in approved if r.status != Status.DROPPED]
    dropped = [r for r in approved if r.status == Status.DROPPED]
    total = sum(r.weight for r in active)
    attained = sum((r.weight * SCORE.get(r.status, Fraction(0)) for r in active), Fraction(0))
    by_category: dict[str, Counter[str]] = {}
    for r in approved:
        by_category.setdefault(r.category, Counter())[r.status] += 1
    return Stats(
        attainment=_ratio(attained, total),
        progress=weighted_progress(rows, progress_day or today),  # the chart's function
        counted=len(active),
        weight_total=total,
        dropped=len(dropped),
        dropped_weight=sum(r.weight for r in dropped),
        drafts=sum(1 for r in rows if not r.approved and r.status != Status.DROPPED),
        overdue=sum(1 for r in active if r.status in OPEN and r.due_date < today),
        by_status=dict(Counter(r.status for r in approved)),
        by_category={category: dict(counts) for category, counts in by_category.items()},
    )


# --------------------------------------------------------------------------- monthly


@dataclass(frozen=True)
class MonthPoint:
    month: dt.date  # first day of the month
    at: dt.date  # the day the value is taken (month end, last day of the year, or today)
    value: Fraction | None


def _month_end(first: dt.date) -> dt.date:
    following = dt.date(first.year + first.month // 12, first.month % 12 + 1, 1)
    return following - dt.timedelta(days=1)


def _progress_at(row: Row, day: dt.date) -> int:
    last = None
    for update in row.updates:  # recorded order: a later record of the same date wins
        if update.date <= day:
            last = update
    return _points(last.status, last.progress) if last is not None else 0


def weighted_progress(rows: Iterable[Row], day: dt.date) -> Fraction | None:
    """Weighted progress on ``day``: approved, not dropped objectives created on or
    before ``day``, each at its last report dated on or before ``day``. None when
    there is no such objective."""
    _date(day, "day")
    present = [r for r in rows if r.approved and r.status != Status.DROPPED and r.created_on <= day]
    total = sum(r.weight for r in present)
    value = sum((Fraction(r.weight * _progress_at(r, day), 100) for r in present), Fraction(0))
    return _ratio(value, total)


def monthly_progress(
    rows: Iterable[Row], *, first_day: dt.date, last_day: dt.date, today: dt.date
) -> list[MonthPoint]:
    """Weighted progress at the end of each month of [first_day, last_day], up to today.

    Counted: approved objectives that are not dropped, from the day they were
    created. A month with no such objective yet has the value None.
    """
    for day, name in ((first_day, "first_day"), (last_day, "last_day"), (today, "today")):
        _date(day, name)
    rows = list(rows)
    end = min(last_day, today)
    points: list[MonthPoint] = []
    month = first_day.replace(day=1)
    while month <= end:
        at = min(_month_end(month), end)
        points.append(MonthPoint(month=month, at=at, value=weighted_progress(rows, at)))
        month = _month_end(month) + dt.timedelta(days=1)
    return points


def as_percent(value: Fraction | None) -> int | None:
    """Whole percent, half rounded up (1/8 -> 13); None stays None."""
    if value is None:
        return None
    scaled = value * 100 + Fraction(1, 2)
    return scaled.numerator // scaled.denominator
