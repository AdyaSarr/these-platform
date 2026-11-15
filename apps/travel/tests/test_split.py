"""travel.split: ST / lab time split over a CIFRE year, gap to the target, monthly
rule. Pure functions on a {date: kind} mapping (no database, no clock)."""

import datetime as dt
from fractions import Fraction

import pytest

from apps.travel import split
from apps.travel.choices import PresenceKind as K
from apps.travel.choices import Side

D = dt.date


def _span(first: dt.date, last: dt.date, kind: K) -> dict[dt.date, K]:
    out = {}
    day = first
    while day <= last:
        out[day] = kind
        day += dt.timedelta(days=1)
    return out


# A CIFRE year that starts on Mon 21 Dec 2026: year 1 runs to 20 Dec 2027, across two
# calendar years (each with its own holidays: 25 Dec 2026, 1 Jan 2027).
CIFRE_START = D(2026, 12, 21)
UNTIL = D(2027, 1, 8)  # year to date: 13 working days (see test_holidays)

DAYS: dict[dt.date, K] = {
    D(2026, 12, 18): K.ROUSSET,  # before the CIFRE year: ignored
    **_span(D(2026, 12, 21), D(2026, 12, 24), K.ROUSSET),  # 4 working days
    D(2026, 12, 25): K.ROUSSET,  # holiday: ignored
    D(2026, 12, 26): K.ROUSSET,  # Saturday: ignored
    D(2026, 12, 28): K.REMOTE,
    **_span(D(2026, 12, 29), D(2026, 12, 31), K.LEAVE),  # 3 working days
    D(2027, 1, 1): K.LIRMM,  # holiday: ignored
    **_span(D(2027, 1, 4), D(2027, 1, 6), K.LIRMM),  # 3 working days
    D(2027, 1, 7): K.CONFERENCE,
    # 8 Jan: not filled in
    D(2027, 1, 11): K.LIRMM,  # after UNTIL: ignored
}


def test_count_across_two_calendar_years() -> None:
    count = split.cifre_year_count(CIFRE_START, 1, DAYS, split.CountingRules(), until=UNTIL)
    assert (count.first, count.last) == (D(2026, 12, 21), UNTIL)
    assert count.working_days == 13
    assert count.by_kind == {K.ROUSSET: 4, K.REMOTE: 1, K.LEAVE: 3, K.LIRMM: 3, K.CONFERENCE: 1}
    assert count.unfilled_days == 1
    # Default rules: remote on the ST side, conference on the lab side.
    assert (count.st_days, count.lab_days) == (5, 4)
    assert count.st_share == Fraction(5, 9)
    assert count.st_percent == Fraction(500, 9)


def test_gap_to_the_target_across_two_calendar_years() -> None:
    count = split.cifre_year_count(CIFRE_START, 1, DAYS, split.CountingRules(), until=UNTIL)
    gap = split.gap_points(count, target_st_percent=70)
    assert gap == Fraction(130, 9)  # 70 - 500/9 = 14.4 points
    assert split.is_alert(gap, threshold=10) is True
    gap = split.gap_points(count, target_st_percent=50)
    assert gap == Fraction(50, 9)  # |50 - 55.6|
    assert split.is_alert(gap, threshold=10) is False


def test_rules_move_remote_and_conference_days() -> None:
    rules = split.CountingRules(remote_counts_as=Side.LAB, conference_counts_as=Side.ST)
    count = split.cifre_year_count(CIFRE_START, 1, DAYS, rules, until=UNTIL)
    assert (count.st_days, count.lab_days) == (5, 4)  # 4 Rousset + 1 conference; 3 + 1
    rules = split.CountingRules(remote_counts_as=Side.LAB, conference_counts_as=Side.LAB)
    count = split.cifre_year_count(CIFRE_START, 1, DAYS, rules, until=UNTIL)
    assert (count.st_days, count.lab_days) == (4, 5)
    assert count.st_share == Fraction(4, 9)


def test_leave_and_unfilled_days_are_excluded_not_counted() -> None:
    only_leave = _span(D(2026, 12, 21), D(2027, 1, 8), K.LEAVE)
    count = split.cifre_year_count(CIFRE_START, 1, only_leave, split.CountingRules(), until=UNTIL)
    assert count.st_days == count.lab_days == 0
    assert count.by_kind == {K.LEAVE: 13}
    assert count.st_share is None
    assert split.gap_points(count, target_st_percent=50) is None
    assert split.is_alert(None, threshold=10) is False


def test_whole_cifre_year_without_until() -> None:
    count = split.cifre_year_count(CIFRE_START, 1, DAYS, split.CountingRules())
    assert (count.first, count.last) == (D(2026, 12, 21), D(2027, 12, 20))
    assert count.working_days == len(split.holidays.working_days(count.first, count.last))
    assert count.unfilled_days == count.working_days - 12


def test_until_before_the_year_gives_an_empty_count() -> None:
    count = split.cifre_year_count(CIFRE_START, 2, DAYS, split.CountingRules(), until=D(2027, 6, 1))
    assert count.working_days == 0
    assert count.by_kind == {}
    assert count.st_share is None


def test_until_after_the_year_is_clipped_to_its_last_day() -> None:
    count = split.cifre_year_count(CIFRE_START, 1, DAYS, split.CountingRules(), until=D(2028, 3, 1))
    assert count.last == D(2027, 12, 20)


def test_whit_monday_counted_only_when_it_is_a_working_day() -> None:
    days = {D(2027, 5, 17): K.LIRMM}
    first, last = D(2027, 5, 1), D(2027, 5, 31)
    assert split.count(days, first, last, split.CountingRules()).lab_days == 0
    rules = split.CountingRules(whit_monday_holiday=False)
    count = split.count(days, first, last, rules)
    assert count.lab_days == 1
    assert count.working_days == 20


@pytest.mark.parametrize(
    ("threshold", "expected"),
    [(10, False), (9, True)],  # an alert when the gap EXCEEDS the threshold
)
def test_alert_is_strict(threshold: int, expected: bool) -> None:
    days = {
        **_span(D(2027, 5, 3), D(2027, 5, 5), K.ROUSSET),  # 3
        D(2027, 5, 7): K.LIRMM,
        D(2027, 5, 10): K.LIRMM,
    }
    count = split.count(days, D(2027, 5, 1), D(2027, 5, 31), split.CountingRules())
    assert count.st_percent == 60
    gap = split.gap_points(count, target_st_percent=50)
    assert gap == 10
    assert split.is_alert(gap, threshold=threshold) is expected


# --------------------------------------------------------------------------- monthly rule


def test_month_lab_days_counts_lirmm_working_days_only() -> None:
    days = {
        D(2027, 5, 3): K.LIRMM,
        D(2027, 5, 4): K.LIRMM,
        D(2027, 5, 6): K.LIRMM,  # Ascension
        D(2027, 5, 8): K.LIRMM,  # Saturday
        D(2027, 5, 10): K.CONFERENCE,  # lab side for the split, not a day AT the lab
        D(2027, 4, 30): K.LIRMM,  # another month
        D(2027, 6, 1): K.LIRMM,
    }
    assert split.month_lab_days(days, 2027, 5, split.CountingRules()) == 2


@pytest.mark.parametrize(
    ("lab_days", "minimum", "expected"),
    [(2, 3, False), (2, 2, True), (0, 0, True), (2, None, None)],
)
def test_month_rule(lab_days: int, minimum: int | None, expected: bool | None) -> None:
    assert split.month_rule_met(lab_days, minimum) is expected


# --------------------------------------------------------------------------- validation


def test_unknown_kind_is_refused() -> None:
    with pytest.raises(ValueError):
        split.count({D(2027, 5, 3): "HOME"}, D(2027, 5, 1), D(2027, 5, 31), split.CountingRules())


def test_a_datetime_key_is_refused() -> None:
    days = {dt.datetime(2027, 5, 3, 9, 0, tzinfo=dt.UTC): K.LIRMM}
    with pytest.raises(TypeError):
        split.count(days, D(2027, 5, 1), D(2027, 5, 31), split.CountingRules())  # type: ignore[arg-type]


def test_reversed_bounds_are_refused() -> None:
    with pytest.raises(ValueError):
        split.count({}, D(2027, 5, 31), D(2027, 5, 1), split.CountingRules())


@pytest.mark.parametrize("target", [-1, 101, 50.0, True])
def test_target_is_an_int_percent(target: object) -> None:
    count = split.count({}, D(2027, 5, 1), D(2027, 5, 31), split.CountingRules())
    with pytest.raises((TypeError, ValueError)):
        split.gap_points(count, target_st_percent=target)  # type: ignore[arg-type]
