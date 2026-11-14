"""Explicit time zones for instants typed in another zone than Paris.

Conference deadlines come with their own zone: "AoE" (Anywhere on Earth, a fixed
UTC-12, common in cryptography) or an IANA name. They are stored in UTC and
shown in Paris time with their original zone. A wall-clock time that does not
exist (skipped by a change to summer time) or that happens twice is refused, as
in meetings.timing: zoneinfo would otherwise pick one silently (``fold=0``).

``Etc/GMT±N`` names are refused: their POSIX sign is inverted (Etc/GMT+12 is
UTC-12), an error waiting to happen; AoE covers the only one in use.
"""

from __future__ import annotations

import datetime as dt
from functools import cache
from zoneinfo import ZoneInfo, available_timezones

from django.core.exceptions import ValidationError
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy

AOE = "AoE"
_AOE_ZONE = dt.timezone(dt.timedelta(hours=-12), AOE)


@cache
def _iana_names() -> frozenset[str]:
    return frozenset(n for n in available_timezones() if not n.startswith("Etc/"))


def _choices() -> list[tuple[str, object]]:
    names = sorted(_iana_names() | {"UTC"})
    return [(AOE, gettext_lazy("AoE — Anywhere on Earth (UTC-12)")), *((n, n) for n in names)]


ZONE_CHOICES = _choices()


def zone(name: str) -> dt.tzinfo:
    """The tzinfo of ``name`` ("AoE", "UTC" or a known IANA name); refused otherwise."""
    if name == AOE:
        return _AOE_ZONE
    if isinstance(name, str) and (name == "UTC" or name in _iana_names()):
        return ZoneInfo(name)
    raise ValidationError(_("Unknown time zone."), code="tz_name")


def local_to_utc(day: dt.date, wall: dt.time, tz_name: str) -> dt.datetime:
    """The UTC instant of ``day`` at ``wall`` in ``tz_name``; nonexistent or ambiguous refused."""
    if isinstance(day, dt.datetime) or not isinstance(day, dt.date):
        raise ValidationError(_("A date is required."), code="date")
    if not isinstance(wall, dt.time) or wall.tzinfo is not None or wall.second or wall.microsecond:
        raise ValidationError(_("A time in hours and minutes is required."), code="time")
    tz = zone(tz_name)
    first = dt.datetime.combine(day, wall, tzinfo=tz)
    instant = first.astimezone(dt.UTC)
    # A skipped time does not come back from UTC: test it first (its folds differ too).
    if instant.astimezone(tz).replace(tzinfo=None) != first.replace(tzinfo=None):
        raise ValidationError(
            _("This time does not exist in this time zone (change to summer time)."),
            code="nonexistent_time",
        )
    if first.utcoffset() != first.replace(fold=1).utcoffset():
        raise ValidationError(
            _("This time happens twice in this time zone (change to winter time)."),
            code="ambiguous_time",
        )
    return instant


def _aware(instant: dt.datetime) -> dt.datetime:
    if not isinstance(instant, dt.datetime) or instant.tzinfo is None:
        raise ValueError("an aware datetime is required")
    return instant


def local_parts(instant: dt.datetime, tz_name: str) -> tuple[dt.date, dt.time]:
    """(date, time to the minute) of ``instant`` in ``tz_name``."""
    local = _aware(instant).astimezone(zone(tz_name))
    return local.date(), local.time().replace(tzinfo=None, second=0, microsecond=0)


def local_date(instant: dt.datetime, tz_name: str) -> dt.date:
    """The date of ``instant`` in ``tz_name`` (the day a deadline counts in)."""
    return _aware(instant).astimezone(zone(tz_name)).date()
