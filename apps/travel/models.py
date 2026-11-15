"""Models of the travel module (M7): presence calendar, CIFRE time-split targets, the
travel policy of a thesis and its missions. See docs/modules/M07-deplacements.md."""

from __future__ import annotations

import uuid
from typing import Any, ClassVar

from django.contrib.postgres.fields import ArrayField
from django.db import models
from django.db.models import F, Q
from django.utils.translation import gettext_lazy as _

from apps.core.confidentiality import ConfidentialityLevel
from apps.core.models import TimeStampedModel

from .access import PresenceAccess, presence_access
from .choices import Destination, Lodging, MissionStatus, Payer, PresenceKind, Side, Transport

MAX_GAP_ALERT_POINTS = 50
MAX_LAB_DAYS_PER_MONTH = 23  # working days of the longest month
MAX_ORDER_LEAD_DAYS = 90
MAX_RECEIPT_GRACE_DAYS = 30
MAX_MISSION_DAYS = 60

# Payer => level: the payer's person (tutor for ST, director for the LIRMM) reads
# what they will grant. Another body: no order in the platform, any level.
PAYER_LEVELS = {
    Payer.ST: ("PUBLIC", "INTERNAL_ST", "CONFIDENTIAL"),
    Payer.LIRMM: ("PUBLIC", "INTERNAL_LIRMM", "CONFIDENTIAL"),
}
DEFAULT_LEVEL = {
    Payer.ST: ConfidentialityLevel.INTERNAL_ST,
    Payer.LIRMM: ConfidentialityLevel.INTERNAL_LIRMM,
    Payer.OTHER: ConfidentialityLevel.INTERNAL_LIRMM,
}


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


class MissionQuerySet(models.QuerySet["Mission"]):
    def visible_to(self, user: Any, thesis: Any) -> MissionQuerySet:
        """The missions of ``thesis`` ``user`` may see (access.py): a co-supervisor
        sees the missions to the LIRMM only."""
        access = presence_access(user, thesis)
        rows = self.filter(thesis=thesis)
        if access is PresenceAccess.NONE:
            return rows.none()
        if access is PresenceAccess.LIRMM_ONLY:
            return rows.filter(destination=Destination.LIRMM)
        return rows


def _default_transports() -> list[str]:
    return [Transport.TRAIN.value]


class Mission(TimeStampedModel):
    """A trip of the student; its texts are sealed (slots travel.Mission.*) at
    ``level``. Never deleted: a mission is cancelled."""

    Status = MissionStatus

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    thesis = models.ForeignKey(
        "accounts.Thesis", on_delete=models.PROTECT, related_name="+", editable=False
    )
    start_date = models.DateField(_("departure"))
    end_date = models.DateField(_("return"))
    destination = models.CharField(_("destination"), max_length=16, choices=Destination.choices)
    edition = models.ForeignKey(
        "conferences.Edition",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="+",
        verbose_name=_("conference"),
    )
    meeting = models.ForeignKey(
        "meetings.Meeting", on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    submission = models.ForeignKey(
        "conferences.Submission",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="+",
    )
    transports = ArrayField(
        models.CharField(max_length=16, choices=Transport.choices),
        verbose_name=_("transport"),
        default=_default_transports,
        size=len(Transport.values),
    )
    lodging = models.CharField(
        _("lodging"), max_length=16, choices=Lodging.choices, default=Lodging.NONE
    )
    payer = models.CharField(_("paid by"), max_length=8, choices=Payer.choices)
    level = models.CharField(
        _("confidentiality"), max_length=20, choices=ConfidentialityLevel.choices
    )
    status = models.CharField(
        _("status"), max_length=20, choices=MissionStatus.choices, default=MissionStatus.PLANNED
    )
    tickets_booked = models.BooleanField(_("tickets booked"), default=False)
    lodging_booked = models.BooleanField(_("lodging booked"), default=False)

    objects = MissionQuerySet.as_manager()

    class Meta:
        verbose_name = _("mission")
        ordering = ("start_date", "created_at")
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.CheckConstraint(
                condition=Q(start_date__lte=F("end_date")), name="travel_mission_dates"
            ),
            models.CheckConstraint(
                condition=Q(destination__in=Destination.values)
                & Q(payer__in=Payer.values)
                & Q(lodging__in=Lodging.values)
                & Q(status__in=MissionStatus.values)
                & Q(level__in=ConfidentialityLevel.values),
                name="travel_mission_choices",
            ),
            # At least one transport (transports[1] is NULL for an empty array: a
            # length test would be NULL too, and a NULL check passes), all known.
            models.CheckConstraint(
                condition=Q(transports__0__isnull=False)
                & Q(transports__contained_by=Transport.values),
                name="travel_mission_transports",
            ),
            models.CheckConstraint(
                condition=Q(payer=Payer.OTHER)
                | Q(payer=Payer.ST, level__in=PAYER_LEVELS[Payer.ST])
                | Q(payer=Payer.LIRMM, level__in=PAYER_LEVELS[Payer.LIRMM]),
                name="travel_mission_payer_level",
            ),
            models.CheckConstraint(
                condition=Q(edition__isnull=True) | Q(destination=Destination.CONFERENCE),
                name="travel_mission_edition",
            ),
        ]

    def __str__(self) -> str:
        return f"mission {self.pk}"

    @property
    def is_closed(self) -> bool:
        """Nothing but expenses and the level changes any more."""
        return self.status in (
            MissionStatus.DONE,
            MissionStatus.REIMBURSED,
            MissionStatus.CANCELLED,
        )
