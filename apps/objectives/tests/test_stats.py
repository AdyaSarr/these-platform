"""objectives.stats: pure, exact (Fraction) statistics of one year's objectives."""

import datetime as dt
from fractions import Fraction
from typing import Any

import pytest

from apps.objectives.models import Category, Status
from apps.objectives.stats import (
    MonthPoint,
    Row,
    Update,
    as_percent,
    compute_stats,
    monthly_progress,
)

D = dt.date
TODAY = D(2027, 3, 15)
DUE = D(2027, 6, 30)
CREATED = D(2026, 12, 1)


def row(
    status: str = Status.IN_PROGRESS,
    *,
    weight: int = 1,
    progress: int = 0,
    approved: bool = True,
    category: str = Category.RESEARCH,
    due: dt.date = DUE,
    created: dt.date = CREATED,
    updates: tuple[Update, ...] = (),
) -> Row:
    if status == Status.ACHIEVED:
        progress = 100
    return Row(
        category=category,
        weight=weight,
        status=status,
        progress=progress,
        due_date=due,
        approved=approved,
        created_on=created,
        updates=updates,
    )


# --------------------------------------------------------------------------- attainment


def test_nominal_case() -> None:
    stats = compute_stats(
        [
            row(Status.ACHIEVED, weight=1),
            row(Status.IN_PROGRESS, weight=2, progress=50),
            row(Status.NOT_ACHIEVED, weight=3, progress=30),
        ],
        TODAY,
    )
    assert stats.attainment == Fraction(1, 6)  # (1*1 + 2*0 + 3*0) / 6
    assert stats.progress == Fraction(1 * 100 + 2 * 50 + 3 * 30, 600)  # 290 / 600
    assert (stats.counted, stats.weight_total) == (3, 6)
    assert isinstance(stats.attainment, Fraction) and isinstance(stats.progress, Fraction)


def test_partial_counts_one_half() -> None:
    stats = compute_stats([row(Status.PARTIAL, weight=2, progress=80)], TODAY)
    assert stats.attainment == Fraction(1, 2)
    assert stats.progress == Fraction(80, 100)  # progress keeps its own value


def test_weights_change_the_rate() -> None:
    stats = compute_stats(
        [row(Status.ACHIEVED, weight=3), row(Status.NOT_ACHIEVED, weight=1)], TODAY
    )
    assert stats.attainment == Fraction(3, 4)  # unweighted it would be 1/2


def test_mixed_results() -> None:
    stats = compute_stats(
        [
            row(Status.ACHIEVED, weight=2),
            row(Status.PARTIAL, weight=2, progress=50),
            row(Status.PLANNED, weight=1),
        ],
        TODAY,
    )
    assert stats.attainment == Fraction(2 * 1 + 2 * Fraction(1, 2), 5)  # 3/5


# --------------------------------------------------------------------------- empty denominators


@pytest.mark.parametrize(
    "rows",
    [
        [],
        [row(Status.ACHIEVED, approved=False)],  # drafts are not counted
        [row(Status.DROPPED, weight=3)],  # only dropped
    ],
)
def test_an_empty_denominator_gives_none_not_zero(rows: list[Row]) -> None:
    stats = compute_stats(rows, TODAY)
    assert stats.attainment is None
    assert stats.progress is None
    assert stats.weight_total == 0


def test_drafts_waiting_for_approval_are_counted_apart() -> None:
    stats = compute_stats(
        [
            row(Status.ACHIEVED),
            row(approved=False),
            row(approved=False),
            row(Status.DROPPED, approved=False),  # a dropped draft waits for nothing
        ],
        TODAY,
    )
    assert stats.drafts == 2
    assert stats.counted == 1
    assert stats.attainment == 1


# --------------------------------------------------------------------------- dropped


def test_dropped_is_shown_as_dropped_and_left_out_of_the_rate() -> None:
    """Acceptance criterion 3: dropped, not deleted, not a failure either."""
    stats = compute_stats(
        [row(Status.ACHIEVED, weight=1), row(Status.DROPPED, weight=3, progress=40)], TODAY
    )
    assert stats.attainment == 1  # not 1/4
    assert stats.progress == 1
    assert (stats.counted, stats.weight_total) == (1, 1)
    assert (stats.dropped, stats.dropped_weight) == (1, 3)
    assert stats.by_status[Status.DROPPED] == 1
    assert stats.by_category[Category.RESEARCH][Status.DROPPED] == 1


def test_breakdown_by_status_and_category() -> None:
    stats = compute_stats(
        [
            row(Status.ACHIEVED, category=Category.PUBLICATION),
            row(Status.PLANNED, category=Category.PUBLICATION),
            row(Status.DROPPED, category=Category.TRAINING),
            row(Status.PLANNED, category=Category.TRAINING, approved=False),  # draft: left out
        ],
        TODAY,
    )
    assert stats.by_status == {Status.ACHIEVED: 1, Status.PLANNED: 1, Status.DROPPED: 1}
    assert stats.by_category == {
        Category.PUBLICATION: {Status.ACHIEVED: 1, Status.PLANNED: 1},
        Category.TRAINING: {Status.DROPPED: 1},
    }


# --------------------------------------------------------------------------- overdue


@pytest.mark.parametrize(
    ("status", "due", "overdue"),
    [
        (Status.IN_PROGRESS, D(2027, 3, 14), True),  # yesterday
        (Status.PLANNED, D(2027, 3, 14), True),
        (Status.IN_PROGRESS, D(2027, 3, 15), False),  # due today: not yet late
        (Status.IN_PROGRESS, D(2027, 3, 16), False),
        (Status.ACHIEVED, D(2027, 1, 1), False),  # done, even late
        (Status.PARTIAL, D(2027, 1, 1), False),
        (Status.NOT_ACHIEVED, D(2027, 1, 1), False),
        (Status.DROPPED, D(2027, 1, 1), False),
    ],
)
def test_overdue(status: str, due: dt.date, overdue: bool) -> None:
    assert compute_stats([row(status, due=due)], TODAY).overdue == int(overdue)


def test_overdue_ignores_drafts() -> None:
    assert compute_stats([row(approved=False, due=D(2027, 1, 1))], TODAY).overdue == 0


def test_today_must_be_a_date() -> None:
    with pytest.raises(TypeError):
        compute_stats([], dt.datetime(2027, 3, 15, 23, 30, tzinfo=dt.UTC))


# --------------------------------------------------------------------------- monthly progress


def test_monthly_progress_uses_the_last_update_before_each_month_end() -> None:
    updates = (
        Update(D(2027, 1, 10), 20, Status.IN_PROGRESS),
        Update(D(2027, 1, 31), 40, Status.IN_PROGRESS),  # last day of January: counts in January
        Update(D(2027, 2, 1), 60, Status.IN_PROGRESS),
    )
    points = monthly_progress(
        [row(updates=updates)], first_day=D(2026, 12, 1), last_day=D(2027, 11, 30), today=TODAY
    )
    assert points == [
        MonthPoint(month=D(2026, 12, 1), at=D(2026, 12, 31), value=Fraction(0)),
        MonthPoint(month=D(2027, 1, 1), at=D(2027, 1, 31), value=Fraction(40, 100)),
        MonthPoint(month=D(2027, 2, 1), at=D(2027, 2, 28), value=Fraction(60, 100)),
        MonthPoint(
            month=D(2027, 3, 1), at=TODAY, value=Fraction(60, 100)
        ),  # current month: up to today
    ]


def test_monthly_progress_is_weighted_and_counts_achieved_as_100() -> None:
    rows = [
        row(weight=3, updates=(Update(D(2027, 1, 5), 70, Status.ACHIEVED),)),
        row(weight=1, updates=(Update(D(2027, 1, 20), 20, Status.IN_PROGRESS),)),
    ]
    (_, january, *_rest) = monthly_progress(
        rows, first_day=D(2026, 12, 1), last_day=D(2027, 11, 30), today=TODAY
    )
    assert january.value == Fraction(3 * 100 + 1 * 20, 400)


def test_an_objective_counts_only_from_its_creation() -> None:
    rows = [
        row(created=D(2026, 12, 5), updates=(Update(D(2026, 12, 20), 50, Status.IN_PROGRESS),)),
        row(created=D(2027, 2, 10)),
    ]
    points = monthly_progress(rows, first_day=D(2026, 12, 1), last_day=D(2027, 11, 30), today=TODAY)
    assert [p.value for p in points] == [
        Fraction(1, 2),
        Fraction(1, 2),
        Fraction(1, 4),
        Fraction(1, 4),
    ]


def test_months_before_any_objective_have_no_value() -> None:
    points = monthly_progress(
        [row(created=D(2027, 2, 10))],
        first_day=D(2026, 12, 1),
        last_day=D(2027, 11, 30),
        today=TODAY,
    )
    assert [p.value for p in points] == [None, None, Fraction(0), Fraction(0)]


def test_monthly_progress_leaves_out_drafts_and_dropped() -> None:
    rows = [row(approved=False, progress=90), row(Status.DROPPED, progress=90)]
    points = monthly_progress(rows, first_day=D(2026, 12, 1), last_day=D(2027, 11, 30), today=TODAY)
    assert all(p.value is None for p in points)


def test_a_finished_year_stops_at_its_last_day() -> None:
    points = monthly_progress(
        [row()], first_day=D(2026, 12, 1), last_day=D(2027, 11, 30), today=D(2028, 5, 1)
    )
    assert len(points) == 12
    assert points[-1] == MonthPoint(month=D(2027, 11, 1), at=D(2027, 11, 30), value=Fraction(0))


def test_a_future_year_has_no_point() -> None:
    assert (
        monthly_progress([row()], first_day=D(2027, 12, 1), last_day=D(2028, 11, 30), today=TODAY)
        == []
    )


def test_a_year_starting_mid_month() -> None:
    points = monthly_progress(
        [row()], first_day=D(2027, 2, 15), last_day=D(2028, 2, 14), today=D(2027, 4, 2)
    )
    assert [(p.month, p.at) for p in points] == [
        (D(2027, 2, 1), D(2027, 2, 28)),
        (D(2027, 3, 1), D(2027, 3, 31)),
        (D(2027, 4, 1), D(2027, 4, 2)),
    ]


# --------------------------------------------------------------------------- display


@pytest.mark.parametrize(
    ("value", "percent"),
    [
        (Fraction(1, 6), 17),
        (Fraction(1, 8), 13),
        (Fraction(1, 200), 1),
        (Fraction(1, 201), 0),
        (Fraction(1), 100),
        (Fraction(0), 0),
        (None, None),
    ],
)
def test_as_percent_rounds_half_up(value: Any, percent: int | None) -> None:
    assert as_percent(value) == percent
