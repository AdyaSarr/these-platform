"""ThesisYear rows: sync_years(), the post_save hook, `manage.py objectives_sync_years`,
and the current year computed on the date in Paris."""

import datetime as dt
import uuid
from io import StringIO
from typing import Any

import pytest
import time_machine
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import IntegrityError, transaction

from apps.accounts.models import Thesis
from apps.accounts.tests.factories import ThesisFactory
from apps.objectives import services
from apps.objectives.models import ThesisYear

pytestmark = pytest.mark.django_db

D = dt.date


def _thesis(**kwargs: Any) -> Thesis:
    fields: dict[str, Any] = {
        "cifre_start_date": D(2026, 12, 1),
        "first_enrollment_date": D(2026, 12, 15),
        "planned_end_date": D(2029, 11, 30),
    }
    fields.update(kwargs)
    return ThesisFactory.create(**fields)


def _rows(thesis: Thesis) -> list[tuple[int, dt.date, dt.date, dt.date, dt.date]]:
    return list(
        ThesisYear.objects.filter(thesis=thesis)
        .order_by("number")
        .values_list("number", "cifre_start", "cifre_end", "academic_start", "academic_end")
    )


EXPECTED = [
    (1, D(2026, 12, 1), D(2027, 11, 30), D(2026, 9, 1), D(2027, 8, 31)),
    (2, D(2027, 12, 1), D(2028, 11, 30), D(2027, 9, 1), D(2028, 8, 31)),
    (3, D(2028, 12, 1), D(2029, 11, 30), D(2028, 9, 1), D(2029, 8, 31)),
]


# --------------------------------------------------------------------------- creation


def test_a_new_thesis_gets_its_years() -> None:
    """post_save of Thesis: no explicit call needed."""
    assert _rows(_thesis()) == EXPECTED


def test_sync_is_idempotent() -> None:
    thesis = _thesis()
    ids = list(ThesisYear.objects.filter(thesis=thesis).order_by("number").values_list("pk"))
    assert services.sync_years(thesis) == services.SyncResult(0, 0, 0, 0)
    assert (
        list(ThesisYear.objects.filter(thesis=thesis).order_by("number").values_list("pk")) == ids
    )
    assert _rows(thesis) == EXPECTED


def test_a_29_february_start() -> None:
    thesis = _thesis(
        cifre_start_date=D(2028, 2, 29),
        first_enrollment_date=D(2028, 3, 1),
        planned_end_date=D(2031, 2, 28),
    )
    assert [r[:3] for r in _rows(thesis)] == [
        (1, D(2028, 2, 29), D(2029, 2, 28)),
        (2, D(2029, 3, 1), D(2030, 2, 28)),
        (3, D(2030, 3, 1), D(2031, 2, 28)),
    ]


def test_the_planned_end_one_day_later_adds_a_fourth_year() -> None:
    thesis = _thesis(planned_end_date=D(2029, 12, 1))
    assert [r[0] for r in _rows(thesis)] == [1, 2, 3, 4]


# --------------------------------------------------------------------------- changes


def test_new_cifre_start_moves_the_dates_and_keeps_the_rows() -> None:
    thesis = _thesis()
    ids = dict(ThesisYear.objects.filter(thesis=thesis).values_list("number", "pk"))
    thesis.cifre_start_date = D(2027, 1, 1)
    thesis.planned_end_date = D(2029, 12, 31)
    thesis.save()  # post_save syncs again
    assert dict(ThesisYear.objects.filter(thesis=thesis).values_list("number", "pk")) == ids
    assert [r[:3] for r in _rows(thesis)] == [
        (1, D(2027, 1, 1), D(2027, 12, 31)),
        (2, D(2028, 1, 1), D(2028, 12, 31)),
        (3, D(2029, 1, 1), D(2029, 12, 31)),
    ]


def test_an_earlier_start_shifting_every_year_does_not_trip_the_overlap_constraint() -> None:
    """Rows are updated one by one: the exclusion constraints are deferred to commit."""
    thesis = _thesis()
    thesis.cifre_start_date = D(2026, 6, 1)
    thesis.planned_end_date = D(2029, 5, 31)
    thesis.save()
    assert [r[1] for r in _rows(thesis)] == [D(2026, 6, 1), D(2027, 6, 1), D(2028, 6, 1)]


def test_extension_and_shortening() -> None:
    thesis = _thesis()
    thesis.planned_end_date = D(2030, 6, 30)
    thesis.save()
    assert [r[0] for r in _rows(thesis)] == [1, 2, 3, 4]
    # update(): no post_save, so the explicit sync below does the work. It reads
    # the thesis from the database (locked), not the instance it is given.
    Thesis.objects.filter(pk=thesis.pk).update(planned_end_date=D(2029, 11, 30))
    result = services.sync_years(thesis)
    assert result == services.SyncResult(created=0, updated=0, deleted=1, kept=0)
    assert _rows(thesis) == EXPECTED


def test_new_enrollment_date_moves_only_the_academic_years() -> None:
    thesis = _thesis()
    thesis.first_enrollment_date = D(2027, 9, 10)
    thesis.save()
    rows = _rows(thesis)
    assert [r[1:3] for r in rows] == [r[1:3] for r in EXPECTED]
    assert [r[3] for r in rows] == [D(2027, 9, 1), D(2028, 9, 1), D(2029, 9, 1)]


def test_raw_saves_from_fixtures_are_ignored() -> None:
    from apps.objectives.apps import sync_years_after_save

    thesis = _thesis()
    ThesisYear.objects.filter(thesis=thesis).delete()
    sync_years_after_save(sender=Thesis, instance=thesis, raw=True)
    assert not ThesisYear.objects.filter(thesis=thesis).exists()


# --------------------------------------------------------------------------- database


def _year(thesis: Thesis, **kwargs: Any) -> ThesisYear:
    fields: dict[str, Any] = {
        "thesis": thesis,
        "number": 9,
        "cifre_start": D(2040, 1, 1),
        "cifre_end": D(2040, 12, 31),
        "academic_start": D(2039, 9, 1),
        "academic_end": D(2040, 8, 31),
    }
    fields.update(kwargs)
    return ThesisYear(**fields)


def test_a_valid_extra_row_passes_the_constraints() -> None:
    with transaction.atomic():
        _year(_thesis()).save()


@pytest.mark.parametrize(
    "overrides",
    [
        {"number": 1},  # same number twice
        {"number": 0},
        {"cifre_start": D(2029, 1, 1), "cifre_end": D(2029, 12, 31)},  # overlaps year 3
        {"academic_start": D(2028, 9, 1), "academic_end": D(2029, 8, 31)},  # overlaps too
        {"cifre_end": D(2039, 12, 31)},  # ends before it starts
        {"academic_end": D(2039, 8, 31)},
    ],
)
def test_constraints_refuse_malformed_rows(overrides: dict[str, Any]) -> None:
    thesis = _thesis()
    with pytest.raises(IntegrityError), transaction.atomic():
        _year(thesis, **overrides).save()
        # Deferred constraints are checked at commit: force it inside the block.
        from django.db import connection

        with connection.cursor() as cursor:
            cursor.execute("SET CONSTRAINTS ALL IMMEDIATE")


def test_years_of_two_theses_may_coincide() -> None:
    """The overlap constraints are per thesis: two theses with the same dates coexist."""
    first, second = _thesis(), _thesis()
    assert _rows(first) == _rows(second) == EXPECTED
    assert ThesisYear.objects.count() == 6


# --------------------------------------------------------------------------- command


def _command(*args: str) -> str:
    out = StringIO()
    call_command("objectives_sync_years", *args, stdout=out)
    return out.getvalue()


def test_command_all_is_idempotent_and_names_no_title() -> None:
    first, second = _thesis(), _thesis(planned_end_date=D(2030, 6, 30))
    ThesisYear.objects.all().delete()  # as before this module existed
    out = _command("--all")
    assert "2 thesis" in out and "7 created" in out
    assert first.title not in out and second.title not in out
    assert _rows(first) == EXPECTED
    out = _command("--all")
    assert "0 created, 0 updated, 0 deleted" in out


def test_command_one_thesis() -> None:
    thesis, other = _thesis(), _thesis()
    ThesisYear.objects.all().delete()
    _command("--thesis", str(thesis.pk))
    assert _rows(thesis) == EXPECTED
    assert not ThesisYear.objects.filter(thesis=other).exists()


@pytest.mark.parametrize(
    "args",
    [
        [],  # nothing chosen
        ["--all", "--thesis", "00000000-0000-0000-0000-000000000000"],
        ["--thesis", str(uuid.uuid4())],  # unknown
        ["--thesis", "not-a-uuid"],
    ],
)
def test_command_refuses_bad_arguments(args: list[str]) -> None:
    with pytest.raises(CommandError):
        _command(*args)


# --------------------------------------------------------------------------- current year (Paris)


@pytest.mark.parametrize(
    ("cifre_start", "instant", "number"),
    [
        # Winter time (UTC+1): 23:59 and 00:30 in Paris around 1 December.
        (D(2026, 12, 1), "2027-11-30T22:59:00+00:00", 1),
        (D(2026, 12, 1), "2027-11-30T23:30:00+00:00", 2),
        # Summer time (UTC+2, since 2027-03-28): around 1 April.
        (D(2026, 4, 1), "2027-03-31T21:59:00+00:00", 1),
        (D(2026, 4, 1), "2027-03-31T22:30:00+00:00", 2),
        # Night of the change to winter time (2027-10-31, 03:00 -> 02:00).
        (D(2026, 11, 1), "2027-10-31T22:59:00+00:00", 1),  # 23:59 Paris (UTC+1)
        (D(2026, 11, 1), "2027-10-31T23:00:00+00:00", 2),  # 00:00 Paris, 1 November
        # Before the CIFRE start.
        (D(2026, 12, 1), "2026-11-30T22:59:00+00:00", None),
        (D(2026, 12, 1), "2026-11-30T23:00:00+00:00", 1),
    ],
)
def test_current_cifre_year_follows_the_date_in_paris(
    cifre_start: dt.date, instant: str, number: int | None
) -> None:
    thesis = _thesis(
        cifre_start_date=cifre_start,
        first_enrollment_date=cifre_start,
        planned_end_date=years_end(cifre_start),
    )
    with time_machine.travel(dt.datetime.fromisoformat(instant), tick=False):
        year = services.current_year(thesis)
    assert (year.number if year else None) == number


@pytest.mark.parametrize(
    ("instant", "number"),
    [
        ("2027-08-31T21:59:00+00:00", 1),  # 23:59 Paris (UTC+2), 31 August
        ("2027-08-31T22:00:00+00:00", 2),  # 00:00 Paris, 1 September
    ],
)
def test_current_academic_year_follows_the_date_in_paris(instant: str, number: int) -> None:
    thesis = _thesis()
    with time_machine.travel(dt.datetime.fromisoformat(instant), tick=False):
        year = services.current_academic_year(thesis)
    assert year is not None and year.number == number


def test_after_the_last_year_there_is_no_current_year() -> None:
    thesis = _thesis()
    with time_machine.travel(dt.datetime(2029, 11, 30, 23, 30, tzinfo=dt.UTC), tick=False):
        assert services.current_year(thesis) is None  # 00:30 Paris, 1 December 2029


def years_end(start: dt.date) -> dt.date:
    """Planned end of a three-year thesis starting on ``start``."""
    from apps.objectives import years

    return years.cifre_bounds(start, 3)[1]
