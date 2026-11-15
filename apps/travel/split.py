"""ST / laboratory time split of a CIFRE thesis, on DATES only (pure functions, no
database, no clock).

Input: a {date: PresenceKind} mapping (the PresenceDay rows the caller is allowed
to count). Only working days (holidays.py) count. ROUSSET is on the ST side and
LIRMM on the lab side; REMOTE and CONFERENCE go to the side the CountingRules say
(TravelPolicy); LEAVE and days not filled in are excluded from the split.

Shares and gaps are exact fractions: the 10-point alert is decided on exact
values, rounding is for display only.
"""

from __future__ import annotations

import datetime as dt
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass, field
from fractions import Fraction

from apps.objectives.years import cifre_bounds

from . import holidays
from .choices import PresenceKind, Side

_FIXED_SIDE: Mapping[PresenceKind, Side] = {
    PresenceKind.ROUSSET: Side.ST,
    PresenceKind.LIRMM: Side.LAB,
}


@dataclass(frozen=True)
class CountingRules:
    remote_counts_as: Side = Side.ST
    conference_counts_as: Side = Side.LAB
    whit_monday_holiday: bool = True

    def side_of(self, kind: PresenceKind) -> Side | None:
        """The side a working day of ``kind`` counts for; None if excluded."""
        if kind == PresenceKind.REMOTE:
            return Side(self.remote_counts_as)
        if kind == PresenceKind.CONFERENCE:
            return Side(self.conference_counts_as)
        return _FIXED_SIDE.get(kind)


@dataclass(frozen=True)
class SplitCount:
    """Counts of the working days of [first, last]."""

    first: dt.date
    last: dt.date
    working_days: int
    st_days: int
    lab_days: int
    unfilled_days: int
    by_kind: Mapping[PresenceKind, int] = field(default_factory=dict)

    @property
    def st_share(self) -> Fraction | None:
        counted = self.st_days + self.lab_days
        return Fraction(self.st_days, counted) if counted else None

    @property
    def st_percent(self) -> Fraction | None:
        share = self.st_share
        return None if share is None else share * 100


def _checked(days: Mapping[dt.date, str]) -> dict[dt.date, PresenceKind]:
    out: dict[dt.date, PresenceKind] = {}
    for day, kind in days.items():
        holidays.require_date(day, "day")
        if kind not in PresenceKind.values:
            raise ValueError(f"unknown presence kind {kind!r}")
        out[day] = PresenceKind(kind)
    return out


def count(
    days: Mapping[dt.date, str], first: dt.date, last: dt.date, rules: CountingRules
) -> SplitCount:
    """Split of the working days of [first, last] (inclusive)."""
    checked = _checked(days)
    worked = holidays.working_days(first, last, whit_monday=rules.whit_monday_holiday)
    by_kind: Counter[PresenceKind] = Counter()
    sides: Counter[Side] = Counter()
    unfilled = 0
    for day in worked:
        kind = checked.get(day)
        if kind is None:
            unfilled += 1
            continue
        by_kind[kind] += 1
        side = rules.side_of(kind)
        if side is not None:
            sides[side] += 1
    return SplitCount(
        first=first,
        last=last,
        working_days=len(worked),
        st_days=sides[Side.ST],
        lab_days=sides[Side.LAB],
        unfilled_days=unfilled,
        by_kind=dict(by_kind),
    )


def cifre_year_count(
    cifre_start: dt.date,
    number: int,
    days: Mapping[dt.date, str],
    rules: CountingRules,
    *,
    until: dt.date | None = None,
) -> SplitCount:
    """Split of CIFRE year ``number``, up to ``until`` (inclusive) if given."""
    first, last = cifre_bounds(cifre_start, number)
    if until is not None:
        holidays.require_date(until, "until")
        last = min(last, until)
    if last < first:  # the year has not started yet
        _checked(days)
        return SplitCount(first, last, working_days=0, st_days=0, lab_days=0, unfilled_days=0)
    return count(days, first, last, rules)


def gap_points(split: SplitCount, *, target_st_percent: int) -> Fraction | None:
    """|ST share - target| in percentage points; None when nothing is counted."""
    if type(target_st_percent) is not int:  # bool and float refused
        raise TypeError("the target is an int percent")
    if not 0 <= target_st_percent <= 100:
        raise ValueError("the target is within 0..100")
    percent = split.st_percent
    return None if percent is None else abs(percent - target_st_percent)


def is_alert(gap: Fraction | None, *, threshold: int) -> bool:
    """True when the gap EXCEEDS the threshold (a gap equal to it is no alert)."""
    return gap is not None and gap > threshold


def month_lab_days(days: Mapping[dt.date, str], year: int, month: int, rules: CountingRules) -> int:
    """LIRMM working days of the month (days AT the lab, whatever the rules say about
    conferences)."""
    first = dt.date(year, month, 1)
    last = (first + dt.timedelta(days=32)).replace(day=1) - dt.timedelta(days=1)
    return count(days, first, last, rules).by_kind.get(PresenceKind.LIRMM, 0)


def month_rule_met(lab_days: int, minimum: int | None) -> bool | None:
    """Whether the monthly rule holds; None when the thesis has no such rule."""
    return None if minimum is None else lab_days >= minimum
