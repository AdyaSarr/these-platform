"""Business logic of objectives (M4). This step: years of a thesis."""

from __future__ import annotations

import datetime as dt
from typing import NamedTuple

from django.db import transaction
from django.db.models import ProtectedError
from django.utils import timezone

from apps.accounts.models import Thesis

from . import years
from .models import ThesisYear


class SyncResult(NamedTuple):
    created: int
    updated: int
    deleted: int
    kept: int  # years past the planned end that still hold objectives


def _expected(thesis: Thesis) -> dict[int, dict[str, dt.date]]:
    count = years.year_count(thesis.cifre_start_date, thesis.planned_end_date)
    expected = {}
    for number in range(1, count + 1):
        cifre_start, cifre_end = years.cifre_bounds(thesis.cifre_start_date, number)
        academic_start, academic_end = years.academic_bounds(thesis.first_enrollment_date, number)
        expected[number] = {
            "cifre_start": cifre_start,
            "cifre_end": cifre_end,
            "academic_start": academic_start,
            "academic_end": academic_end,
        }
    return expected


def sync_years(thesis: Thesis) -> SyncResult:
    """Make the ThesisYear rows match the thesis dates; idempotent.

    Existing rows keep their id (objectives point to them) and get new dates;
    missing years are created; a year past the planned end is deleted unless
    objectives still point to it (PROTECT): it is then kept and counted.
    The thesis row is locked: two concurrent syncs of one thesis run one
    after the other. No vault call: safe after an audit() in the caller's
    transaction (create_thesis).
    """
    with transaction.atomic():
        locked = Thesis.objects.select_for_update().get(pk=thesis.pk)
        expected = _expected(locked)
        created = updated = deleted = kept = 0
        existing = {y.number: y for y in ThesisYear.objects.filter(thesis=locked)}
        for number, fields in expected.items():
            year = existing.get(number)
            if year is None:
                ThesisYear.objects.create(thesis=locked, number=number, **fields)
                created += 1
            elif any(getattr(year, name) != value for name, value in fields.items()):
                for name, value in fields.items():
                    setattr(year, name, value)
                year.save(update_fields=[*fields, "updated_at"])
                updated += 1
        for number, year in sorted(existing.items()):
            if number in expected:
                continue
            try:
                with transaction.atomic():
                    year.delete()
                deleted += 1
            except ProtectedError:
                kept += 1
    return SyncResult(created, updated, deleted, kept)


def _year_containing(thesis: Thesis, start: str, end: str) -> ThesisYear | None:
    today = timezone.localdate()  # the date in Paris (TIME_ZONE)
    return (
        ThesisYear.objects.filter(thesis=thesis, **{f"{start}__lte": today, f"{end}__gte": today})
        .order_by("number")
        .first()
    )


def current_year(thesis: Thesis) -> ThesisYear | None:
    """The CIFRE year containing today's date in Paris; None outside every year."""
    return _year_containing(thesis, "cifre_start", "cifre_end")


def current_academic_year(thesis: Thesis) -> ThesisYear | None:
    """The academic year containing today's date in Paris; None outside every year."""
    return _year_containing(thesis, "academic_start", "academic_end")
