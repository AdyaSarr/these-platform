"""French public holidays (metropolitan France) and working days, on DATES only
(pure functions, no database, no clock, no network, no third-party calendar).

The eleven holidays of the Code du travail (art. L3133-1), without the local ones
of Alsace-Moselle (Good Friday, 26 December): neither ST Rousset nor the LIRMM is
there. Whit Monday is a holiday by default; it is often the "journée de
solidarité" an employer may choose to work, hence ``whit_monday``
(TravelPolicy.whit_monday_holiday).

Working day = Monday to Friday, not a holiday. Callers convert an instant to a
date in Paris first (timezone.localdate()): every function here refuses a datetime.
"""

from __future__ import annotations

import datetime as dt
from functools import lru_cache

# The Gregorian computus below is valid from the reform to 4099 (Meeus).
FIRST_YEAR = 1583
LAST_YEAR = 4099

_ONE_DAY = dt.timedelta(days=1)


def require_date(value: dt.date, name: str) -> dt.date:
    # datetime is a subclass of date: refused explicitly (an instant in UTC is not a
    # date in Paris).
    if isinstance(value, dt.datetime) or not isinstance(value, dt.date):
        raise TypeError(f"{name} must be a datetime.date, not {type(value).__name__}")
    return value


def _year(year: int) -> int:
    if type(year) is not int:  # bool refused
        raise TypeError("a year is an int")
    if not FIRST_YEAR <= year <= LAST_YEAR:
        raise ValueError(f"year must be within {FIRST_YEAR}..{LAST_YEAR}")
    return year


def easter_sunday(year: int) -> dt.date:
    """Gregorian Easter Sunday ("anonymous" algorithm, Meeus / Jones / Butcher)."""
    y = _year(year)
    a = y % 19  # place in the 19-year Metonic cycle
    b, c = divmod(y, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30  # epact correction
    i, k = divmod(c, 4)
    weekday = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * weekday) // 451
    month, day = divmod(h + weekday - 7 * m + 114, 31)
    return dt.date(y, month, day + 1)


@lru_cache(maxsize=64)
def public_holidays(year: int, *, whit_monday: bool = True) -> frozenset[dt.date]:
    """The public holidays of ``year`` in metropolitan France."""
    easter = easter_sunday(year)
    days = {
        dt.date(year, 1, 1),  # jour de l'An
        easter + dt.timedelta(days=1),  # lundi de Pâques
        dt.date(year, 5, 1),  # fête du Travail
        dt.date(year, 5, 8),  # Victoire 1945
        easter + dt.timedelta(days=39),  # Ascension
        dt.date(year, 7, 14),  # fête nationale
        dt.date(year, 8, 15),  # Assomption
        dt.date(year, 11, 1),  # Toussaint
        dt.date(year, 11, 11),  # Armistice
        dt.date(year, 12, 25),  # Noël
    }
    if whit_monday:
        days.add(easter + dt.timedelta(days=50))  # lundi de Pentecôte
    return frozenset(days)


def is_working_day(day: dt.date, *, whit_monday: bool = True) -> bool:
    """Monday to Friday and not a public holiday."""
    require_date(day, "day")
    return day.weekday() < 5 and day not in public_holidays(day.year, whit_monday=whit_monday)


def working_days(first: dt.date, last: dt.date, *, whit_monday: bool = True) -> list[dt.date]:
    """Working days of [first, last] (inclusive), in order."""
    require_date(first, "first")
    require_date(last, "last")
    if first > last:
        raise ValueError("first must not be after last")
    out: list[dt.date] = []
    day = first
    while day <= last:
        if is_working_day(day, whit_monday=whit_monday):
            out.append(day)
        day += _ONE_DAY
    return out
