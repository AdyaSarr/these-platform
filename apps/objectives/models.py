"""Objectives (M4): years of a thesis, objectives, revisions, progress, comments.

No free text in these tables: titles, descriptions, success criteria, revision
reasons and values, progress notes and comments are sealed by the vault
(vault.SealedText, slots objectives.*), at the level of their objective.
"""

from __future__ import annotations

import uuid
from typing import ClassVar

from django.conf import settings
from django.contrib.postgres.constraints import ExclusionConstraint
from django.contrib.postgres.fields import DateRangeField, RangeOperators
from django.db import models
from django.db.models import Deferrable, Func, Q
from django.utils.translation import gettext_lazy as _

from apps.core.confidentiality import ConfidentialityLevel
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


# --------------------------------------------------------------------------- objectives


class Category(models.TextChoices):
    RESEARCH = "RESEARCH", _("Research")
    PUBLICATION = "PUBLICATION", _("Publication")
    TRAINING = "TRAINING", _("Training")
    ADMIN = "ADMIN", _("Administrative")
    OTHER = "OTHER", _("Other")


class Status(models.TextChoices):
    PLANNED = "PLANNED", _("Planned")
    IN_PROGRESS = "IN_PROGRESS", _("In progress")
    ACHIEVED = "ACHIEVED", _("Achieved")
    PARTIAL = "PARTIAL", _("Partially achieved")
    NOT_ACHIEVED = "NOT_ACHIEVED", _("Not achieved")
    DROPPED = "DROPPED", _("Dropped")


# INTERNAL_ST is left out: the director, who approves objectives, cannot read it.
OBJECTIVE_LEVELS = (
    ConfidentialityLevel.PUBLIC,
    ConfidentialityLevel.INTERNAL_LIRMM,
    ConfidentialityLevel.CONFIDENTIAL,
)


class Objective(TimeStampedModel):
    """An objective of one CIFRE year. Its texts are sealed (slots objectives.Objective.*).

    Approved once by the director; afterwards every change of its definition
    needs a reason and creates ObjectiveRevision rows, and sets
    ``changed_since_approval`` until the director acknowledges it. Never deleted:
    a dropped objective stays, with its reason.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    thesis = models.ForeignKey(
        "accounts.Thesis", on_delete=models.PROTECT, related_name="objectives", editable=False
    )
    year = models.ForeignKey(
        ThesisYear, on_delete=models.PROTECT, related_name="objectives", verbose_name=_("year")
    )
    category = models.CharField(_("category"), max_length=20, choices=Category.choices)
    due_date = models.DateField(_("due date"))
    weight = models.PositiveSmallIntegerField(_("weight"), default=1)
    status = models.CharField(
        _("status"), max_length=20, choices=Status.choices, default=Status.PLANNED, editable=False
    )
    progress = models.PositiveSmallIntegerField(_("progress"), default=0, editable=False)
    level = models.CharField(
        _("confidentiality"),
        max_length=20,
        choices=[(level.value, level.label) for level in OBJECTIVE_LEVELS],
        default=ConfidentialityLevel.PUBLIC,
        editable=False,
    )
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="+",
        editable=False,
    )
    approved_at = models.DateTimeField(null=True, blank=True, editable=False)
    changed_since_approval = models.BooleanField(default=False, editable=False)
    # A result (ACHIEVED, PARTIAL, NOT_ACHIEVED) declared or withdrawn after
    # approval: counted at once, shown "to acknowledge" until the director does.
    result_declared = models.BooleanField(default=False, editable=False)
    acknowledged_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="+",
        editable=False,
    )
    acknowledged_at = models.DateTimeField(null=True, blank=True, editable=False)

    class Meta:
        verbose_name = _("objective")
        verbose_name_plural = _("objectives")
        ordering: ClassVar[list[str]] = ["year__number", "due_date", "created_at"]
        indexes: ClassVar[list[models.Index]] = [models.Index(fields=["thesis", "year"])]
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.CheckConstraint(
                condition=Q(weight__gte=1, weight__lte=3), name="objectives_objective_weight"
            ),
            models.CheckConstraint(
                condition=Q(progress__gte=0, progress__lte=100),
                name="objectives_objective_progress",
            ),
            models.CheckConstraint(
                condition=Q(level__in=[level.value for level in OBJECTIVE_LEVELS]),
                name="objectives_objective_level",
            ),
            models.CheckConstraint(
                condition=Q(category__in=Category.values), name="objectives_objective_category"
            ),
            models.CheckConstraint(
                condition=Q(status__in=Status.values), name="objectives_objective_status"
            ),
            models.CheckConstraint(
                condition=Q(approved_by__isnull=True, approved_at__isnull=True)
                | Q(approved_by__isnull=False, approved_at__isnull=False),
                name="objectives_objective_approval_pair",
            ),
            models.CheckConstraint(
                condition=Q(acknowledged_by__isnull=True, acknowledged_at__isnull=True)
                | Q(acknowledged_by__isnull=False, acknowledged_at__isnull=False),
                name="objectives_objective_acknowledgement_pair",
            ),
            models.CheckConstraint(
                condition=Q(changed_since_approval=False) | Q(approved_at__isnull=False),
                name="objectives_objective_changed_only_if_approved",
            ),
            models.CheckConstraint(
                condition=Q(result_declared=False) | Q(approved_at__isnull=False),
                name="objectives_objective_result_only_if_approved",
            ),
            models.CheckConstraint(
                condition=~Q(status=Status.ACHIEVED) | Q(progress=100),
                name="objectives_objective_achieved_is_100",
            ),
            models.CheckConstraint(
                condition=~Q(status=Status.PLANNED) | Q(progress=0),
                name="objectives_objective_planned_has_no_progress",
            ),
        ]

    def __str__(self) -> str:
        # Never the title: str() ends up in logs and error reports.
        return f"objective {self.pk}"

    @property
    def is_approved(self) -> bool:
        return self.approved_at is not None


RESULT_STATUSES = (Status.ACHIEVED, Status.PARTIAL, Status.NOT_ACHIEVED)


class RevisedField(models.TextChoices):
    TITLE = "title", _("title")
    DESCRIPTION = "description", _("description")
    SUCCESS_CRITERION = "success_criterion", _("success criterion")
    CATEGORY = "category", _("category")
    DUE_DATE = "due_date", _("due date")
    WEIGHT = "weight", _("weight")
    LEVEL = "level", _("confidentiality")
    STATUS = "status", _("status")


TEXT_FIELDS = (RevisedField.TITLE, RevisedField.DESCRIPTION, RevisedField.SUCCESS_CRITERION)


class ObjectiveRevision(TimeStampedModel):
    """One changed field of an objective. Reason always sealed; old and new values
    sealed for a text field (slots old_value / new_value), in clear otherwise."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    thesis = models.ForeignKey(
        "accounts.Thesis", on_delete=models.PROTECT, related_name="+", editable=False
    )
    objective = models.ForeignKey(
        Objective, on_delete=models.PROTECT, related_name="revisions", editable=False
    )
    changed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+", editable=False
    )
    field = models.CharField(max_length=20, choices=RevisedField.choices, editable=False)
    old_clear = models.CharField(max_length=20, blank=True, editable=False)
    new_clear = models.CharField(max_length=20, blank=True, editable=False)
    # Set by the director's acknowledgement (services.acknowledge_changes): exact,
    # where comparing timestamps with approved_at would not be.
    acknowledged_at = models.DateTimeField(null=True, blank=True, editable=False)

    class Meta:
        verbose_name = _("objective revision")
        verbose_name_plural = _("objective revisions")
        ordering: ClassVar[list[str]] = ["objective", "created_at"]
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.CheckConstraint(
                condition=Q(field__in=RevisedField.values), name="objectives_revision_field"
            ),
            models.CheckConstraint(
                condition=~Q(field__in=[f.value for f in TEXT_FIELDS])
                | Q(old_clear="", new_clear=""),
                name="objectives_revision_text_values_sealed",
            ),
        ]

    def __str__(self) -> str:
        return f"revision {self.pk}"


class ProgressUpdate(TimeStampedModel):
    """A dated progress report; its note is sealed, its evidence an optional Document."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    thesis = models.ForeignKey(
        "accounts.Thesis", on_delete=models.PROTECT, related_name="+", editable=False
    )
    objective = models.ForeignKey(
        Objective, on_delete=models.PROTECT, related_name="progress_updates", editable=False
    )
    date = models.DateField(_("date"), editable=False)
    progress = models.PositiveSmallIntegerField(_("progress"), editable=False)
    status = models.CharField(max_length=20, choices=Status.choices, editable=False)
    evidence = models.ForeignKey(
        "vault.Document",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="+",
        editable=False,
    )

    class Meta:
        verbose_name = _("progress update")
        verbose_name_plural = _("progress updates")
        ordering: ClassVar[list[str]] = ["objective", "date", "created_at"]
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.CheckConstraint(
                condition=Q(progress__gte=0, progress__lte=100), name="objectives_progress_range"
            ),
            models.CheckConstraint(
                condition=Q(status__in=Status.values) & ~Q(status=Status.DROPPED),
                name="objectives_progress_status",
            ),
        ]

    def __str__(self) -> str:
        return f"progress update {self.pk}"


class ObjectiveComment(TimeStampedModel):
    """A comment of the supervision (OBJECTIVE_COMMENT only); its body is sealed."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    thesis = models.ForeignKey(
        "accounts.Thesis", on_delete=models.PROTECT, related_name="+", editable=False
    )
    objective = models.ForeignKey(
        Objective, on_delete=models.PROTECT, related_name="comments", editable=False
    )
    author = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+", editable=False
    )

    class Meta:
        verbose_name = _("objective comment")
        verbose_name_plural = _("objective comments")
        ordering: ClassVar[list[str]] = ["objective", "created_at"]

    def __str__(self) -> str:
        return f"comment {self.pk}"


class WeeklySummaryLog(TimeStampedModel):
    """One weekly summary e-mail: (thesis, recipient, week_start) is claimed ONCE.

    As ReminderLog (M3): the run that inserts the row queues the e-mail, a
    concurrent or repeated run finds it. ``week_start`` is the Monday of the
    run's date in Paris.
    """

    class Status(models.TextChoices):
        PENDING = "PENDING", _("Pending")
        SENT = "SENT", _("Sent")
        FAILED = "FAILED", _("Failed")
        SKIPPED = "SKIPPED", _("Not sent: no longer concerned or due")

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    thesis = models.ForeignKey(
        "accounts.Thesis", on_delete=models.PROTECT, related_name="+", editable=False
    )
    recipient = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+", editable=False
    )
    week_start = models.DateField(editable=False)
    status = models.CharField(
        max_length=10, choices=Status.choices, default=Status.PENDING, editable=False
    )
    attempts = models.PositiveSmallIntegerField(default=0, editable=False)
    sent_at = models.DateTimeField(null=True, editable=False)

    class Meta:
        verbose_name = _("weekly summary")
        verbose_name_plural = _("weekly summaries")
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.UniqueConstraint(
                fields=["thesis", "recipient", "week_start"], name="objectives_summary_once"
            ),
            models.CheckConstraint(
                condition=Q(week_start__iso_week_day=1), name="objectives_summary_monday"
            ),
            models.CheckConstraint(
                condition=(Q(status="SENT") & Q(sent_at__isnull=False))
                | (~Q(status="SENT") & Q(sent_at__isnull=True)),
                name="objectives_summary_sent_at",
            ),
        ]

    def __str__(self) -> str:
        return f"weekly summary {self.pk}"
