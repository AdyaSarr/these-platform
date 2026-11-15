"""travel.holidays: French public holidays (metropolitan France) and working days,
pure date arithmetic (no database, no clock, no network)."""

import datetime as dt

import pytest
from dateutil.easter import EASTER_WESTERN, easter  # reference implementation, tests only

from apps.travel import holidays

D = dt.date


# --------------------------------------------------------------------------- Easter


@pytest.mark.parametrize(
    ("year", "expected"),
    [
        (2000, D(2000, 4, 23)),
        (2008, D(2008, 3, 23)),
        (2011, D(2011, 4, 24)),  # latest possible date
        (2019, D(2019, 4, 21)),
        (2024, D(2024, 3, 31)),
        (2025, D(2025, 4, 20)),
        (2026, D(2026, 4, 5)),
        (2027, D(2027, 3, 28)),
        (2028, D(2028, 4, 16)),
        (2029, D(2029, 4, 1)),
        (2030, D(2030, 4, 21)),
        (2038, D(2038, 4, 25)),
        (2285, D(2285, 3, 22)),  # earliest possible date
    ],
)
def test_easter_sunday_known_dates(year: int, expected: dt.date) -> None:
    assert holidays.easter_sunday(year) == expected


def test_easter_sunday_matches_an_independent_implementation() -> None:
    for year in range(1900, 2201):
        assert holidays.easter_sunday(year) == easter(year, EASTER_WESTERN), year


@pytest.mark.parametrize("year", [1582, 4100, -1])
def test_easter_sunday_outside_the_gregorian_range_is_refused(year: int) -> None:
    with pytest.raises(ValueError):
        holidays.easter_sunday(year)


@pytest.mark.parametrize("year", [2027.0, "2027", True])
def test_easter_sunday_needs_an_int(year: object) -> None:
    with pytest.raises(TypeError):
        holidays.easter_sunday(year)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- holidays

HOLIDAYS_2027 = {
    D(2027, 1, 1),  # jour de l'An
    D(2027, 3, 29),  # lundi de Pâques
    D(2027, 5, 1),  # fête du Travail
    D(2027, 5, 6),  # Ascension (Pâques + 39)
    D(2027, 5, 8),  # Victoire 1945
    D(2027, 5, 17),  # lundi de Pentecôte (Pâques + 50)
    D(2027, 7, 14),
    D(2027, 8, 15),
    D(2027, 11, 1),
    D(2027, 11, 11),
    D(2027, 12, 25),
}


def test_public_holidays_of_2027() -> None:
    assert holidays.public_holidays(2027) == frozenset(HOLIDAYS_2027)


def test_whit_monday_can_be_a_working_day() -> None:
    expected = frozenset(HOLIDAYS_2027 - {D(2027, 5, 17)})
    assert holidays.public_holidays(2027, whit_monday=False) == expected


def test_eleven_holidays_every_year() -> None:
    for year in range(2026, 2040):
        days = holidays.public_holidays(year)
        assert len(days) == 11
        assert {d.year for d in days} == {year}


# --------------------------------------------------------------------------- working days


@pytest.mark.parametrize(
    ("day", "expected"),
    [
        (D(2027, 5, 4), True),  # Tuesday
        (D(2027, 5, 8), False),  # Saturday (and a holiday)
        (D(2027, 5, 9), False),  # Sunday
        (D(2027, 5, 6), False),  # Ascension, a Thursday
        (D(2027, 1, 1), False),  # Friday, jour de l'An
        (D(2027, 5, 17), False),  # Whit Monday by default
    ],
)
def test_is_working_day(day: dt.date, expected: bool) -> None:
    assert holidays.is_working_day(day) is expected


def test_whit_monday_working_when_the_policy_says_so() -> None:
    assert holidays.is_working_day(D(2027, 5, 17), whit_monday=False) is True


def test_working_days_of_may_2027() -> None:
    # 21 weekdays; Ascension (Thu 6) and Whit Monday (17) fall on weekdays, 1 and 8 May
    # on Saturdays.
    days = holidays.working_days(D(2027, 5, 1), D(2027, 5, 31))
    assert len(days) == 19
    assert D(2027, 5, 6) not in days
    assert D(2027, 5, 17) not in days
    assert days == sorted(days)
    assert len(holidays.working_days(D(2027, 5, 1), D(2027, 5, 31), whit_monday=False)) == 20


def test_working_days_across_the_new_year() -> None:
    # Mon 21 Dec 2026 to Fri 8 Jan 2027: 15 weekdays, minus 25 Dec and 1 Jan.
    assert len(holidays.working_days(D(2026, 12, 21), D(2027, 1, 8))) == 13


def test_working_days_bounds_are_inclusive_and_ordered() -> None:
    assert holidays.working_days(D(2027, 5, 4), D(2027, 5, 4)) == [D(2027, 5, 4)]
    with pytest.raises(ValueError):
        holidays.working_days(D(2027, 5, 5), D(2027, 5, 4))


def test_a_datetime_is_not_a_date() -> None:
    instant = dt.datetime(2027, 5, 4, 23, 30, tzinfo=dt.UTC)
    with pytest.raises(TypeError):
        holidays.is_working_day(instant)
    with pytest.raises(TypeError):
        holidays.working_days(instant, D(2027, 5, 31))
