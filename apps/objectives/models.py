"""Objectives (M4). This step: the years of a thesis."""

from __future__ import annotations

import uuid
from typing import ClassVar

from django.contrib.postgres.constraints import ExclusionConstraint
from django.contrib.postgres.fields import DateRangeField, RangeOperators
from django.db import models
from django.db.models import Deferrable, Func
from django.utils.translation import gettext_lazy as _

from apps.core.models import TimeStampedModel


class _DateRange(Func):
    """daterange(start, end, '[]'): inclusive bounds, as stored."""

    function = "DATERANGE"
    output_field = DateRangeField()


def _no_overlap(start: str, end: str, name: str) -> ExclusionConstraint:
    # Deferred to commit: sync_years() moves years one row at a time.
    return ExclusionConstraint(
        name=name,
        expressions=[
            ("thesis", RangeOperators.EQUAL),
            (_DateRange(start, end, models.Value("[]")), RangeOperators.OVERLAPS),
        ],
        deferrable=Deferrable.DEFERRED,
    )


class ThesisYear(TimeStampedModel):
    """Year ``number`` of a thesis, in both calendars (objectives.years).

    Computed from the thesis dates by services.sync_years(), never typed in.
    Bounds are inclusive dates. ``Objective.year`` points to the CIFRE year.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    thesis = models.ForeignKey(
        "accounts.Thesis", on_delete=models.PROTECT, related_name="years", editable=False
    )
    number = models.PositiveSmallIntegerField(_("year"), editable=False)
    cifre_start = models.DateField(_("CIFRE year start"), editable=False)
    cifre_end = models.DateField(_("CIFRE year end"), editable=False)
    academic_start = models.DateField(_("academic year start"), editable=False)
    academic_end = models.DateField(_("academic year end"), editable=False)

    class Meta:
        verbose_name = _("thesis year")
        verbose_name_plural = _("thesis years")
        ordering: ClassVar[list[str]] = ["thesis", "number"]
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.UniqueConstraint(fields=["thesis", "number"], name="objectives_year_number"),
            models.CheckConstraint(
                condition=models.Q(number__gte=1), name="objectives_year_number_from_1"
            ),
            models.CheckConstraint(
                condition=models.Q(cifre_start__lte=models.F("cifre_end")),
                name="objectives_year_cifre_ordered",
            ),
            models.CheckConstraint(
                condition=models.Q(academic_start__lte=models.F("academic_end")),
                name="objectives_year_academic_ordered",
            ),
            _no_overlap("cifre_start", "cifre_end", "objectives_year_cifre_no_overlap"),
            _no_overlap("academic_start", "academic_end", "objectives_year_academic_no_overlap"),
        ]

    def __str__(self) -> str:
        return f"year {self.number} of thesis {self.thesis_id}"
