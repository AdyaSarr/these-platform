"""Times of meetings: typed in Paris wall-clock time, stored in UTC.

A wall-clock time that does not exist (skipped when summer time starts) or that
happens twice (repeated when it ends) is refused: zoneinfo would otherwise pick
one of them silently (``fold=0``). Date bounds of a meeting: from three months
before the CIFRE start to twelve months after the planned end (inclusive),
computed by meeting_bounds() only.
"""

from __future__ import annotations

import calendar
import datetime as dt
from zoneinfo import ZoneInfo

from django.conf import settings
from django.core.exceptions import ValidationError
from django.utils.translation import gettext as _

MONTHS_BEFORE_START = 3
MONTHS_AFTER_END = 12


def paris() -> ZoneInfo:
    return ZoneInfo(settings.TIME_ZONE)


def add_months(day: dt.date, months: int) -> dt.date:
    """``day`` moved by ``months`` calendar months, clamped to the end of the month."""
    if isinstance(day, dt.datetime) or not isinstance(day, dt.date):
        raise TypeError("day must be a date")
    if type(months) is not int:
        raise TypeError("months must be an int")
    index = day.year * 12 + (day.month - 1) + months
    year, month = divmod(index, 12)
    last = calendar.monthrange(year, month + 1)[1]
    return dt.date(year, month + 1, min(day.day, last))


def meeting_bounds(cifre_start: dt.date, planned_end: dt.date) -> tuple[dt.date, dt.date]:
    """First and last allowed date (inclusive, in Paris) of a meeting of a thesis."""
    return (
        add_months(cifre_start, -MONTHS_BEFORE_START),
        add_months(planned_end, MONTHS_AFTER_END),
    )


def check_date(day: dt.date, cifre_start: dt.date, planned_end: dt.date) -> None:
    first, last = meeting_bounds(cifre_start, planned_end)
    if not first <= day <= last:
        raise ValidationError(
            _("The date must be between %(first)s and %(last)s.")
            % {"first": first.strftime("%d/%m/%Y"), "last": last.strftime("%d/%m/%Y")},
            code="out_of_bounds",
        )


def paris_datetime(day: dt.date, wall: dt.time) -> dt.datetime:
    """The UTC instant of ``day`` at ``wall`` in Paris; nonexistent or ambiguous refused."""
    if isinstance(day, dt.datetime) or not isinstance(day, dt.date):
        raise ValidationError(_("A date is required."), code="date")
    if not isinstance(wall, dt.time) or wall.tzinfo is not None or wall.second or wall.microsecond:
        raise ValidationError(_("A time in hours and minutes is required."), code="time")
    zone = paris()
    first = dt.datetime.combine(day, wall, tzinfo=zone)
    instant = first.astimezone(dt.UTC)
    # A skipped time does not come back from UTC (02:30 -> 03:30): test it first,
    # since its two folds also have different offsets.
    if instant.astimezone(zone).replace(tzinfo=None) != first.replace(tzinfo=None):
        raise ValidationError(
            _("This time does not exist in Paris (change to summer time): choose another."),
            code="nonexistent_time",
        )
    if first.utcoffset() != first.replace(fold=1).utcoffset():
        raise ValidationError(
            _("This time happens twice in Paris (change to winter time): choose another."),
            code="ambiguous_time",
        )
    return instant


def paris_parts(instant: dt.datetime) -> tuple[dt.date, dt.time]:
    """(date, time) of ``instant`` in Paris, for forms."""
    local = instant.astimezone(paris())
    return local.date(), local.time().replace(tzinfo=None, second=0, microsecond=0)
