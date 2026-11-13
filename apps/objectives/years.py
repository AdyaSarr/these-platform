"""Years of a thesis, on DATES only (pure functions, no database, no clock).

Two calendars run side by side (docs/specs, "Deux calendriers parallèles"):

- CIFRE year n: from the n-1-th anniversary of the CIFRE start date to the day
  before the n-th one. ``Objective.year`` counts in CIFRE years (the 36-month
  clock of the contract, docs/decisions.md).
- academic year n: 1 September to 31 August; year 1 is the one containing the
  date of first enrollment.

Bounds are inclusive dates. An anniversary of 29 February in a year without
one falls on 1 March, so that CIFRE year ends on 28 February (same rule as
art. 641 of the French code of civil procedure: the period expires on the last
day of the month). Years are therefore contiguous, each 365 or 366 days long.

Callers convert an instant to a date in Paris first (timezone.localdate()):
every function here refuses a datetime.
"""

from __future__ import annotations

import datetime as dt

ACADEMIC_START_MONTH = 9  # 1 September
MAX_YEARS = 8  # CIFRE contract (3 years) plus any extension of the doctorate


def _date(value: dt.date, name: str) -> dt.date:
    # datetime is a subclass of date: refused explicitly (an instant in UTC is
    # not a date in Paris).
    if isinstance(value, dt.datetime) or not isinstance(value, dt.date):
        raise TypeError(f"{name} must be a datetime.date, not {type(value).__name__}")
    return value


def _number(value: int) -> int:
    if type(value) is not int:  # bool refused
        raise TypeError("a year number is an int")
    if value < 1:
        raise ValueError("year numbers start at 1")
    return value


def add_years(day: dt.date, n: int) -> dt.date:
    """The ``n``-th anniversary of ``day``; 29 February -> 1 March in a common year."""
    _date(day, "day")
    if type(n) is not int or n < 0:
        raise ValueError("n must be a non-negative int")
    try:
        return day.replace(year=day.year + n)
    except ValueError:  # 29 February of a common year
        return dt.date(day.year + n, 3, 1)


def cifre_bounds(cifre_start: dt.date, number: int) -> tuple[dt.date, dt.date]:
    """First and last day (inclusive) of CIFRE year ``number``."""
    _date(cifre_start, "cifre_start")
    _number(number)
    first = add_years(cifre_start, number - 1)
    last = add_years(cifre_start, number) - dt.timedelta(days=1)
    return first, last


def _academic_origin(first_enrollment: dt.date) -> int:
    """Calendar year in which academic year 1 starts."""
    _date(first_enrollment, "first_enrollment")
    month = first_enrollment.month
    return first_enrollment.year if month >= ACADEMIC_START_MONTH else first_enrollment.year - 1


def academic_bounds(first_enrollment: dt.date, number: int) -> tuple[dt.date, dt.date]:
    """1 September and 31 August (inclusive) of academic year ``number``."""
    origin = _academic_origin(first_enrollment)
    _number(number)
    start_year = origin + number - 1
    first = dt.date(start_year, ACADEMIC_START_MONTH, 1)
    return first, dt.date(start_year + 1, ACADEMIC_START_MONTH, 1) - dt.timedelta(days=1)


def year_count(cifre_start: dt.date, planned_end: dt.date) -> int:
    """How many CIFRE years cover [cifre_start, planned_end]."""
    _date(cifre_start, "cifre_start")
    _date(planned_end, "planned_end")
    if planned_end <= cifre_start:
        raise ValueError("the planned end must be after the CIFRE start")
    for number in range(1, MAX_YEARS + 1):
        if cifre_bounds(cifre_start, number)[1] >= planned_end:
            return number
    raise ValueError(f"a thesis has at most {MAX_YEARS} years")


def cifre_year_of(cifre_start: dt.date, day: dt.date) -> int | None:
    """Number of the CIFRE year containing ``day``; None before the CIFRE start."""
    _date(cifre_start, "cifre_start")
    _date(day, "day")
    if day < cifre_start:
        return None
    number = day.year - cifre_start.year  # anniversaries passed, or one too many
    if add_years(cifre_start, number) > day:
        number -= 1
    return number + 1


def academic_year_of(first_enrollment: dt.date, day: dt.date) -> int | None:
    """Number of the academic year containing ``day``; None before academic year 1."""
    origin = _academic_origin(first_enrollment)
    _date(day, "day")
    start_year = day.year if day.month >= ACADEMIC_START_MONTH else day.year - 1
    number = start_year - origin + 1
    return number if number >= 1 else None
