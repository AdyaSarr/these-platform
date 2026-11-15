"""Explicit time zones of external deadlines (core.timezones): AoE and IANA."""

from __future__ import annotations

import datetime as dt

import pytest
from django.core.exceptions import ValidationError

from apps.core import timezones as tz

D, T, UTC = dt.date, dt.time, dt.UTC


def test_aoe_is_utc_minus_twelve_all_year() -> None:
    assert tz.local_to_utc(D(2027, 1, 15), T(23, 59), "AoE") == dt.datetime(
        2027, 1, 16, 11, 59, tzinfo=UTC
    )
    assert tz.local_to_utc(D(2027, 7, 15), T(23, 59), "AoE") == dt.datetime(
        2027, 7, 16, 11, 59, tzinfo=UTC
    )


def test_aoe_in_paris_moves_with_paris_summer_time() -> None:
    winter = tz.local_to_utc(D(2027, 1, 15), T(23, 59), "AoE")
    summer = tz.local_to_utc(D(2027, 7, 15), T(23, 59), "AoE")
    assert tz.local_parts(winter, "Europe/Paris") == (D(2027, 1, 16), T(12, 59))
    assert tz.local_parts(summer, "Europe/Paris") == (D(2027, 7, 16), T(13, 59))


@pytest.mark.parametrize(
    ("day", "paris"),
    [
        # 2027-03-14: Los Angeles is on summer time, Paris not yet (until 03-28).
        (D(2027, 3, 20), T(8, 0)),
        (D(2027, 4, 2), T(9, 0)),
        # 2027-10-31: Paris back to winter time, Los Angeles until 11-07.
        (D(2027, 11, 3), T(8, 0)),
        (D(2027, 11, 10), T(9, 0)),
    ],
)
def test_los_angeles_noon_in_paris_across_the_mismatched_weeks(
    day: dt.date, paris: dt.time
) -> None:
    instant = tz.local_to_utc(day, T(0, 0), "America/Los_Angeles")
    assert tz.local_parts(instant, "Europe/Paris") == (day, paris)


def test_nonexistent_and_ambiguous_local_times_are_refused() -> None:
    with pytest.raises(ValidationError) as exc:
        tz.local_to_utc(D(2027, 3, 14), T(2, 30), "America/Los_Angeles")
    assert exc.value.code == "nonexistent_time"
    with pytest.raises(ValidationError) as exc:
        tz.local_to_utc(D(2027, 11, 7), T(1, 30), "America/Los_Angeles")
    assert exc.value.code == "ambiguous_time"
    # AoE has no change: any wall time exists once.
    tz.local_to_utc(D(2027, 3, 28), T(2, 30), "AoE")


@pytest.mark.parametrize("name", ["", "aoe", "Mars/Olympus", "Etc/GMT+12", "Etc/GMT-1", 12, None])
def test_unknown_or_confusing_zones_are_refused(name: object) -> None:
    with pytest.raises(ValidationError) as exc:
        tz.zone(name)  # type: ignore[arg-type]
    assert exc.value.code == "tz_name"


def test_known_zones() -> None:
    assert tz.zone("UTC").utcoffset(None) == dt.timedelta(0)
    assert tz.zone("AoE").utcoffset(None) == dt.timedelta(hours=-12)
    assert tz.zone("Asia/Tokyo")
    choices = [value for value, _ in tz.ZONE_CHOICES]
    assert choices[0] == "AoE" and "Europe/Paris" in choices and "UTC" in choices
    assert not any(c.startswith("Etc/") for c in choices)
    assert len(choices) == len(set(choices))


def test_local_date_is_the_date_in_the_zone() -> None:
    # 2027-01-15 07:00 in Paris is still 2027-01-14 in AoE.
    instant = dt.datetime(2027, 1, 15, 6, 0, tzinfo=UTC)
    assert tz.local_date(instant, "AoE") == D(2027, 1, 14)
    assert tz.local_date(instant, "Europe/Paris") == D(2027, 1, 15)


def test_inputs_are_checked() -> None:
    with pytest.raises(ValidationError):
        tz.local_to_utc(dt.datetime(2027, 1, 1), T(1, 0), "AoE")
    with pytest.raises(ValidationError):
        tz.local_to_utc(D(2027, 1, 1), T(1, 0, 30), "AoE")
    with pytest.raises(ValueError):
        tz.local_date(dt.datetime(2027, 1, 1), "AoE")  # naive
