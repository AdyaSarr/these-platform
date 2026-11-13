"""objectives.years: CIFRE and academic years, pure date arithmetic (no database)."""

import datetime as dt
import random

import pytest

from apps.objectives import years

D = dt.date


# --------------------------------------------------------------------------- anniversaries


@pytest.mark.parametrize(
    ("start", "n", "expected"),
    [
        (D(2026, 12, 1), 1, D(2027, 12, 1)),
        (D(2026, 12, 31), 1, D(2027, 12, 31)),
        (D(2027, 1, 1), 3, D(2030, 1, 1)),
        (D(2028, 2, 29), 1, D(2029, 3, 1)),  # no 29 February: the day after the 28th
        (D(2028, 2, 29), 4, D(2032, 2, 29)),  # leap year again
        (D(2027, 2, 28), 1, D(2028, 2, 28)),  # a 28 February stays a 28 February
        (D(2026, 3, 1), 0, D(2026, 3, 1)),
    ],
)
def test_add_years(start: dt.date, n: int, expected: dt.date) -> None:
    assert years.add_years(start, n) == expected


def test_add_years_refuses_datetimes_and_negative_counts() -> None:
    with pytest.raises(TypeError):
        years.add_years(dt.datetime(2026, 12, 1, 10, 0), 1)
    with pytest.raises(ValueError):
        years.add_years(D(2026, 12, 1), -1)


# --------------------------------------------------------------------------- CIFRE years


@pytest.mark.parametrize(
    ("start", "number", "bounds"),
    [
        (D(2026, 12, 1), 1, (D(2026, 12, 1), D(2027, 11, 30))),
        (D(2026, 12, 1), 2, (D(2027, 12, 1), D(2028, 11, 30))),
        (D(2026, 12, 1), 3, (D(2028, 12, 1), D(2029, 11, 30))),
        (D(2027, 1, 1), 1, (D(2027, 1, 1), D(2027, 12, 31))),
        (D(2026, 3, 1), 3, (D(2028, 3, 1), D(2029, 2, 28))),  # year 3 ends before a leap day
        (D(2028, 2, 29), 1, (D(2028, 2, 29), D(2029, 2, 28))),
        (D(2028, 2, 29), 2, (D(2029, 3, 1), D(2030, 2, 28))),
        (D(2028, 2, 29), 4, (D(2031, 3, 1), D(2032, 2, 28))),
        (D(2028, 2, 29), 5, (D(2032, 2, 29), D(2033, 2, 28))),
    ],
)
def test_cifre_bounds(start: dt.date, number: int, bounds: tuple[dt.date, dt.date]) -> None:
    assert years.cifre_bounds(start, number) == bounds


def test_cifre_years_are_contiguous_and_one_year_long() -> None:
    rng = random.Random(20261118)  # noqa: S311 - test data only
    for _ in range(2_000):
        start = D(2020, 1, 1) + dt.timedelta(days=rng.randrange(0, 4_000))
        previous_end = start - dt.timedelta(days=1)
        for number in range(1, 6):
            first, last = years.cifre_bounds(start, number)
            assert first == previous_end + dt.timedelta(days=1), (start, number)
            assert (last - first).days + 1 in (365, 366), (start, number)
            previous_end = last


@pytest.mark.parametrize("number", [0, -1, True])
def test_year_numbers_start_at_one(number: int) -> None:
    with pytest.raises((TypeError, ValueError)):
        years.cifre_bounds(D(2026, 12, 1), number)
    with pytest.raises((TypeError, ValueError)):
        years.academic_bounds(D(2026, 12, 1), number)


# --------------------------------------------------------------------------- academic years


@pytest.mark.parametrize(
    ("enrollment", "number", "bounds"),
    [
        (D(2026, 9, 1), 1, (D(2026, 9, 1), D(2027, 8, 31))),  # first day of the year
        (D(2026, 8, 31), 1, (D(2025, 9, 1), D(2026, 8, 31))),  # last day of the previous one
        (D(2026, 12, 15), 1, (D(2026, 9, 1), D(2027, 8, 31))),
        (D(2027, 1, 10), 1, (D(2026, 9, 1), D(2027, 8, 31))),  # January: same year as September
        (D(2026, 12, 15), 2, (D(2027, 9, 1), D(2028, 8, 31))),
        (D(2026, 12, 15), 3, (D(2028, 9, 1), D(2029, 8, 31))),  # leap February inside
    ],
)
def test_academic_bounds(enrollment: dt.date, number: int, bounds: tuple[dt.date, dt.date]) -> None:
    assert years.academic_bounds(enrollment, number) == bounds


# --------------------------------------------------------------------------- how many years


@pytest.mark.parametrize(
    ("planned_end", "count"),
    [
        (D(2027, 11, 30), 1),
        (D(2029, 11, 30), 3),  # exactly the end of CIFRE year 3
        (D(2029, 12, 1), 4),  # one day more: a fourth year
        (D(2028, 6, 30), 2),
        (D(2026, 12, 2), 1),
    ],
)
def test_year_count_covers_the_planned_end(planned_end: dt.date, count: int) -> None:
    assert years.year_count(D(2026, 12, 1), planned_end) == count


def test_year_count_is_bounded() -> None:
    with pytest.raises(ValueError, match="at most"):
        years.year_count(D(2026, 12, 1), D(2040, 1, 1))
    with pytest.raises(ValueError):
        years.year_count(D(2026, 12, 1), D(2026, 12, 1))  # end not after start


# --------------------------------------------------------------------------- which year a day is in


@pytest.mark.parametrize(
    ("day", "number"),
    [
        (D(2026, 11, 30), None),  # before the CIFRE start
        (D(2026, 12, 1), 1),
        (D(2027, 11, 30), 1),
        (D(2027, 12, 1), 2),
        (D(2029, 11, 30), 3),
        (D(2029, 12, 1), 4),  # past the planned years: still numbered
    ],
)
def test_cifre_year_of(day: dt.date, number: int | None) -> None:
    assert years.cifre_year_of(D(2026, 12, 1), day) == number


def test_cifre_year_of_a_29_february_start() -> None:
    start = D(2028, 2, 29)
    assert years.cifre_year_of(start, D(2029, 2, 28)) == 1
    assert years.cifre_year_of(start, D(2029, 3, 1)) == 2
    assert years.cifre_year_of(start, D(2032, 2, 28)) == 4
    assert years.cifre_year_of(start, D(2032, 2, 29)) == 5


@pytest.mark.parametrize(
    ("day", "number"),
    [
        (D(2026, 8, 31), None),
        (D(2026, 9, 1), 1),
        (D(2027, 8, 31), 1),
        (D(2027, 9, 1), 2),
    ],
)
def test_academic_year_of(day: dt.date, number: int | None) -> None:
    assert years.academic_year_of(D(2026, 12, 15), day) == number


def test_year_of_agrees_with_the_bounds() -> None:
    rng = random.Random(20261119)  # noqa: S311 - test data only
    for _ in range(500):
        start = D(2020, 1, 1) + dt.timedelta(days=rng.randrange(0, 4_000))
        for number in range(1, 5):
            first, last = years.cifre_bounds(start, number)
            assert years.cifre_year_of(start, first) == number
            assert years.cifre_year_of(start, last) == number
            first, last = years.academic_bounds(start, number)
            assert years.academic_year_of(start, first) == number
            assert years.academic_year_of(start, last) == number


def test_datetimes_are_refused_everywhere() -> None:
    """Years are dates in Paris: a datetime (UTC or not) must be converted by the caller."""
    instant = dt.datetime(2027, 11, 30, 23, 30, tzinfo=dt.UTC)
    for call in (
        lambda: years.cifre_year_of(D(2026, 12, 1), instant),
        lambda: years.academic_year_of(D(2026, 12, 1), instant),
        lambda: years.cifre_bounds(instant, 1),
        lambda: years.year_count(D(2026, 12, 1), instant),
    ):
        with pytest.raises(TypeError):
            call()
