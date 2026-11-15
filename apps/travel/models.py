"""Models of the travel module (M7): presence calendar, CIFRE time-split targets and
the travel policy of a thesis. See docs/modules/M07-deplacements.md."""

from __future__ import annotations

import uuid
from typing import Any, ClassVar

from django.db import models
from django.db.models import F, Q
from django.utils.translation import gettext_lazy as _

from apps.core.models import TimeStampedModel

from .access import PresenceAccess, presence_access
from .choices import PresenceKind, Side

MAX_GAP_ALERT_POINTS = 50
MAX_LAB_DAYS_PER_MONTH = 23  # working days of the longest month
MAX_ORDER_LEAD_DAYS = 90
MAX_RECEIPT_GRACE_DAYS = 30


class PresenceDayQuerySet(models.QuerySet["PresenceDay"]):
    def visible_to(self, user: Any, thesis: Any) -> PresenceDayQuerySet:
        """The rows of ``thesis`` ``user`` may see (access.py): kinds are filtered
        here, before anything is counted or rendered."""
        access = presence_access(user, thesis)
        rows = self.filter(thesis=thesis)
        if access is PresenceAccess.NONE:
            return rows.none()
        if access is PresenceAccess.LIRMM_ONLY:
            return rows.filter(kind=PresenceKind.LIRMM)
        return rows


class PresenceDay(TimeStampedModel):
    """Where the student is (or was) on one day of a thesis; a whole day."""

    Kind = PresenceKind

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    thesis = models.ForeignKey(
        "accounts.Thesis", on_delete=models.PROTECT, related_name="+", editable=False
    )
    day = models.DateField(_("day"))
    kind = models.CharField(_("kind"), max_length=16, choices=PresenceKind.choices)

    objects = PresenceDayQuerySet.as_manager()

    class Meta:
        verbose_name = _("presence day")
        ordering = ("day",)
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.UniqueConstraint(fields=("thesis", "day"), name="travel_presence_unique_day"),
            models.CheckConstraint(
                condition=Q(kind__in=PresenceKind.values), name="travel_presence_kind"
            ),
        ]

    def __str__(self) -> str:
        return f"presence {self.pk}"


class TimeSplitTarget(TimeStampedModel):
    """The ST / lab split of one CIFRE year, from the collaboration contract (M3),
    which the platform does not read: set by the director or the tutor."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    thesis = models.ForeignKey(
        "accounts.Thesis", on_delete=models.PROTECT, related_name="+", editable=False
    )
    cifre_year = models.PositiveSmallIntegerField(_("CIFRE year"))
    st_percent = models.PositiveSmallIntegerField(_("ST share (percent)"))
    lab_percent = models.PositiveSmallIntegerField(_("laboratory share (percent)"))

    class Meta:
        verbose_name = _("time-split target")
        ordering = ("cifre_year",)
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.UniqueConstraint(
                fields=("thesis", "cifre_year"), name="travel_target_unique_year"
            ),
            models.CheckConstraint(condition=Q(cifre_year__gte=1), name="travel_target_year"),
            models.CheckConstraint(
                condition=Q(st_percent__lte=100)
                & Q(lab_percent__lte=100)
                & Q(st_percent=100 - F("lab_percent")),
                name="travel_target_sum",
            ),
        ]

    def __str__(self) -> str:
        return f"target {self.pk}"


class TravelPolicy(TimeStampedModel):
    """Counting rules (director or tutor) and reminder periods (student) of a thesis."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    thesis = models.OneToOneField(
        "accounts.Thesis", on_delete=models.PROTECT, related_name="travel_policy", editable=False
    )
    # Counting rules.
    remote_counts_as = models.CharField(
        _("remote work counts for"), max_length=3, choices=Side.choices, default=Side.ST
    )
    conference_counts_as = models.CharField(
        _("conferences count for"), max_length=3, choices=Side.choices, default=Side.LAB
    )
    whit_monday_holiday = models.BooleanField(_("Whit Monday is a holiday"), default=True)
    gap_alert_points = models.PositiveSmallIntegerField(_("alert above (points)"), default=10)
    min_lab_days_per_month = models.PositiveSmallIntegerField(
        _("minimum LIRMM days per month"), null=True, blank=True
    )
    # Reminder periods.
    order_lead_days = models.PositiveSmallIntegerField(
        _("mission order to request (days before)"), default=15
    )
    receipt_grace_days = models.PositiveSmallIntegerField(
        _("receipts expected (days after)"), default=3
    )

    RULE_FIELDS: ClassVar[tuple[str, ...]] = (
        "remote_counts_as",
        "conference_counts_as",
        "whit_monday_holiday",
        "gap_alert_points",
        "min_lab_days_per_month",
    )
    REMINDER_FIELDS: ClassVar[tuple[str, ...]] = ("order_lead_days", "receipt_grace_days")

    class Meta:
        verbose_name = _("travel policy")
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.CheckConstraint(
                condition=Q(remote_counts_as__in=Side.values)
                & Q(conference_counts_as__in=Side.values),
                name="travel_policy_sides",
            ),
            models.CheckConstraint(
                condition=Q(gap_alert_points__gte=1, gap_alert_points__lte=MAX_GAP_ALERT_POINTS),
                name="travel_policy_gap",
            ),
            models.CheckConstraint(
                condition=Q(min_lab_days_per_month__isnull=True)
                | Q(min_lab_days_per_month__lte=MAX_LAB_DAYS_PER_MONTH),
                name="travel_policy_month_rule",
            ),
            models.CheckConstraint(
                condition=Q(order_lead_days__lte=MAX_ORDER_LEAD_DAYS)
                & Q(receipt_grace_days__lte=MAX_RECEIPT_GRACE_DAYS),
                name="travel_policy_reminders",
            ),
        ]

    def __str__(self) -> str:
        return f"travel policy {self.pk}"
