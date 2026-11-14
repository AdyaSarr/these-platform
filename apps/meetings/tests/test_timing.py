"""Paris wall-clock times and the date bounds of a meeting (meetings.timing)."""

from __future__ import annotations

import datetime as dt

import pytest
from django.core.exceptions import ValidationError

from apps.meetings import timing

D = dt.date
T = dt.time
UTC = dt.UTC


# --------------------------------------------------------------------------- months


@pytest.mark.parametrize(
    ("day", "months", "expected"),
    [
        (D(2026, 6, 1), -3, D(2026, 3, 1)),
        (D(2026, 1, 15), -3, D(2025, 10, 15)),
        (D(2026, 5, 31), -3, D(2026, 2, 28)),  # clamped to the end of February
        (D(2028, 5, 31), -3, D(2028, 2, 29)),  # leap year
        (D(2029, 11, 30), 12, D(2030, 11, 30)),
        (D(2028, 2, 29), 12, D(2029, 2, 28)),
        (D(2026, 12, 31), 2, D(2027, 2, 28)),
        (D(2026, 3, 31), 0, D(2026, 3, 31)),
    ],
)
def test_add_months_clamps_to_the_last_day_of_the_month(
    day: dt.date, months: int, expected: dt.date
) -> None:
    assert timing.add_months(day, months) == expected


def test_add_months_refuses_datetimes_and_non_ints() -> None:
    with pytest.raises(TypeError):
        timing.add_months(dt.datetime(2026, 1, 1), 1)
    with pytest.raises(TypeError):
        timing.add_months(D(2026, 1, 1), True)


# --------------------------------------------------------------------------- bounds


def test_bounds_are_three_months_before_the_cifre_start_and_a_year_after_the_end() -> None:
    first, last = timing.meeting_bounds(D(2026, 6, 1), D(2029, 5, 31))
    assert (first, last) == (D(2026, 3, 1), D(2030, 5, 31))


@pytest.mark.parametrize(
    ("day", "ok"),
    [
        (D(2026, 2, 28), False),
        (D(2026, 3, 1), True),
        (D(2030, 5, 31), True),
        (D(2030, 6, 1), False),
    ],
)
def test_check_date_inclusive_bounds(day: dt.date, ok: bool) -> None:
    if ok:
        timing.check_date(day, D(2026, 6, 1), D(2029, 5, 31))
    else:
        with pytest.raises(ValidationError) as exc:
            timing.check_date(day, D(2026, 6, 1), D(2029, 5, 31))
        assert exc.value.code == "out_of_bounds"


# --------------------------------------------------------------------------- Paris times


def test_winter_and_summer_offsets() -> None:
    assert timing.paris_datetime(D(2027, 1, 12), T(14, 0)) == dt.datetime(
        2027, 1, 12, 13, 0, tzinfo=UTC
    )
    assert timing.paris_datetime(D(2027, 7, 12), T(14, 0)) == dt.datetime(
        2027, 7, 12, 12, 0, tzinfo=UTC
    )


def test_midnight_paris_is_the_previous_day_in_utc() -> None:
    assert timing.paris_datetime(D(2027, 1, 12), T(0, 30)) == dt.datetime(
        2027, 1, 11, 23, 30, tzinfo=UTC
    )


@pytest.mark.parametrize("wall", [T(2, 0), T(2, 30), T(2, 59)])
def test_a_time_skipped_by_summer_time_is_refused(wall: dt.time) -> None:
    # 2027-03-28: clocks go from 02:00 to 03:00 in Paris.
    with pytest.raises(ValidationError) as exc:
        timing.paris_datetime(D(2027, 3, 28), wall)
    assert exc.value.code == "nonexistent_time"


@pytest.mark.parametrize("wall", [T(2, 0), T(2, 30), T(2, 59)])
def test_a_time_repeated_by_winter_time_is_refused(wall: dt.time) -> None:
    # 2027-10-31: clocks go from 03:00 back to 02:00 in Paris.
    with pytest.raises(ValidationError) as exc:
        timing.paris_datetime(D(2027, 10, 31), wall)
    assert exc.value.code == "ambiguous_time"


def test_times_around_the_changes_are_accepted() -> None:
    assert timing.paris_datetime(D(2027, 3, 28), T(1, 59)) == dt.datetime(
        2027, 3, 28, 0, 59, tzinfo=UTC
    )
    assert timing.paris_datetime(D(2027, 3, 28), T(3, 0)) == dt.datetime(
        2027, 3, 28, 1, 0, tzinfo=UTC
    )
    assert timing.paris_datetime(D(2027, 10, 31), T(3, 0)) == dt.datetime(
        2027, 10, 31, 2, 0, tzinfo=UTC
    )


def test_seconds_and_tz_aware_times_are_refused() -> None:
    with pytest.raises(ValidationError):
        timing.paris_datetime(D(2027, 1, 12), T(14, 0, 30))
    with pytest.raises(ValidationError):
        timing.paris_datetime(D(2027, 1, 12), T(14, 0, tzinfo=UTC))


def test_paris_parts_round_trip() -> None:
    start = dt.datetime(2027, 7, 12, 12, 0, tzinfo=UTC)
    assert timing.paris_parts(start) == (D(2027, 7, 12), T(14, 0))
