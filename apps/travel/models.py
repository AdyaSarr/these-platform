"""Models of the travel module (M7): presence calendar, CIFRE time-split targets, the
travel policy of a thesis and its missions. See docs/modules/M07-deplacements.md."""

from __future__ import annotations

import uuid
from collections.abc import Collection
from typing import Any, ClassVar, Self, TypeVar

from django.conf import settings
from django.contrib.postgres.fields import ArrayField
from django.db import models
from django.db.models import F, Q
from django.utils.translation import gettext_lazy as _

from apps.core.confidentiality import ConfidentialityLevel
from apps.core.models import TimeStampedModel

from .access import PresenceAccess, presence_access
from .choices import (
    FLAT_RATE_CATEGORIES,
    Destination,
    ExpenseCategory,
    Lodging,
    MissionStatus,
    NotificationKind,
    NotificationStatus,
    OrderDecision,
    OrderRefusalReason,
    Payer,
    PresenceKind,
    Side,
    Transport,
    Window,
)

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
        _("mission approval to request (days before)"), default=15
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
    # The OFFICIAL mission order, issued outside the platform (ST's HR tool, the
    # LIRMM's manager); MissionOrder is the supervisor's approval.
    official_order_obtained = models.BooleanField(
        _("official mission order obtained"), default=False
    )
    official_order_document = models.ForeignKey(
        "vault.Document",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="+",
        verbose_name=_("official mission order"),
    )

    # Raised whenever the dates, the destination or the status change, so that a
    # calendar that imported the .ics updates its event (RFC 5545 SEQUENCE).
    ics_sequence = models.PositiveIntegerField(default=0, editable=False)

    objects = MissionQuerySet.as_manager()

    ICS_FIELDS: ClassVar[tuple[str, ...]] = ("start_date", "end_date", "destination", "status")

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

    @classmethod
    def from_db(
        cls, db: str | None, field_names: Collection[str], values: Collection[Any], **kw: Any
    ) -> Self:
        instance = super().from_db(db, field_names, values, **kw)
        if all(name in field_names for name in cls.ICS_FIELDS):
            instance._ics_seen = tuple(getattr(instance, n) for n in cls.ICS_FIELDS)
        return instance

    def save(self, *args: Any, **kwargs: Any) -> None:
        current = tuple(getattr(self, n) for n in self.ICS_FIELDS)
        seen = getattr(self, "_ics_seen", None)
        if seen is not None and seen != current:
            self.ics_sequence += 1
        super().save(*args, **kwargs)
        self._ics_seen = current

    @property
    def is_closed(self) -> bool:
        """Nothing but expenses and the level changes any more."""
        return self.status in (
            MissionStatus.DONE,
            MissionStatus.REIMBURSED,
            MissionStatus.CANCELLED,
        )


# --------------------------------------------------------------------------- orders


class HistoryError(Exception):
    """A decided mission order is never modified or deleted."""


_M = TypeVar("_M", bound=models.Model)


class _HistoryQuerySet(models.QuerySet[_M]):
    def update(self, **kwargs: Any) -> int:
        raise HistoryError("a history is never updated in bulk")

    def delete(self) -> tuple[int, dict[str, int]]:
        raise HistoryError("a history is never deleted")

    def bulk_update(self, *args: Any, **kwargs: Any) -> int:
        raise HistoryError("a history is never updated in bulk")


ORDER_PAYERS = (Payer.ST.value, Payer.LIRMM.value)  # another body: no order here
_DECIDED = ("GRANTED", "REFUSED")


class MissionOrder(TimeStampedModel):
    """A request of a mission order, decided once by the payer's person (granted,
    refused) or withdrawn by the student; decided = history. Its snapshot is what
    was granted: dates, destination, payer. Texts (comment, estimate) are sealed at
    the mission's level (slots travel.MissionOrder.*)."""

    Decision = OrderDecision
    Reason = OrderRefusalReason

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    thesis = models.ForeignKey(
        "accounts.Thesis", on_delete=models.PROTECT, related_name="+", editable=False
    )
    mission = models.ForeignKey(
        Mission, on_delete=models.PROTECT, related_name="orders", editable=False
    )
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+", editable=False
    )
    payer = models.CharField(max_length=8, choices=Payer.choices, editable=False)
    snapshot_start = models.DateField(editable=False)
    snapshot_end = models.DateField(editable=False)
    snapshot_destination = models.CharField(
        max_length=16, choices=Destination.choices, editable=False
    )
    decision = models.CharField(
        _("decision"), max_length=12, choices=OrderDecision.choices, default=OrderDecision.PENDING
    )
    reason = models.CharField(
        _("reason"), max_length=24, choices=OrderRefusalReason.choices, blank=True, default=""
    )
    decided_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    decided_at = models.DateTimeField(null=True, blank=True)

    objects = _HistoryQuerySet.as_manager()

    class Meta:
        verbose_name = _("mission approval")
        ordering: ClassVar[list[str]] = ["created_at", "pk"]
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.CheckConstraint(
                condition=Q(decision__in=OrderDecision.values)
                & Q(payer__in=ORDER_PAYERS)
                & Q(snapshot_destination__in=Destination.values)
                & (Q(reason="") | Q(reason__in=OrderRefusalReason.values)),
                name="travel_order_choices",
            ),
            models.UniqueConstraint(
                fields=["mission"], condition=Q(decision="PENDING"), name="travel_order_one_pending"
            ),
            models.CheckConstraint(
                condition=(Q(decision="PENDING") & Q(decided_at__isnull=True))
                | (~Q(decision="PENDING") & Q(decided_at__isnull=False)),
                name="travel_order_decided_at",
            ),
            models.CheckConstraint(
                condition=(Q(decision__in=_DECIDED) & Q(decided_by__isnull=False))
                | (~Q(decision__in=_DECIDED) & Q(decided_by__isnull=True)),
                name="travel_order_decided_by",
            ),
            models.CheckConstraint(
                condition=Q(decided_by__isnull=True) | ~Q(decided_by=F("requested_by")),
                name="travel_order_not_by_requester",
            ),
            models.CheckConstraint(
                condition=(Q(decision="REFUSED") & ~Q(reason=""))
                | (~Q(decision="REFUSED") & Q(reason="")),
                name="travel_order_reason_iff_refused",
            ),
        ]

    def save(self, *args: Any, **kwargs: Any) -> None:
        if not self._state.adding:
            stored = MissionOrder.objects.filter(pk=self.pk).values_list("decision", flat=True)
            if stored.first() != OrderDecision.PENDING:
                raise HistoryError("a decided mission order is never modified")
        super().save(*args, **kwargs)

    def delete(self, *args: Any, **kwargs: Any) -> tuple[int, dict[str, int]]:
        raise HistoryError("a mission order is never deleted")

    def __str__(self) -> str:
        return f"mission order {self.pk}"

    def snapshot_matches(self, mission: Mission) -> bool:
        return (self.snapshot_start, self.snapshot_end, self.snapshot_destination, self.payer) == (
            mission.start_date,
            mission.end_date,
            mission.destination,
            mission.payer,
        )


# --------------------------------------------------------------------------- expenses


class Expense(TimeStampedModel):
    """One expense line of a mission; amounts sealed at the mission's level (slots
    travel.Expense.*). A flat-rate line (MEALS, OTHER) needs no receipt."""

    Category = ExpenseCategory

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    thesis = models.ForeignKey(
        "accounts.Thesis", on_delete=models.PROTECT, related_name="+", editable=False
    )
    mission = models.ForeignKey(
        Mission, on_delete=models.PROTECT, related_name="expenses", editable=False
    )
    category = models.CharField(_("category"), max_length=16, choices=ExpenseCategory.choices)
    flat_rate = models.BooleanField(_("flat rate (no receipt)"), default=False)

    class Meta:
        verbose_name = _("expense")
        ordering: ClassVar[list[str]] = ["created_at", "pk"]
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.CheckConstraint(
                condition=Q(category__in=ExpenseCategory.values)
                & (Q(flat_rate=False) | Q(category__in=FLAT_RATE_CATEGORIES)),
                name="travel_expense_category_flat_rate",
            ),
        ]

    def __str__(self) -> str:
        return f"expense {self.pk}"


class ExpenseReceipt(TimeStampedModel):
    """A receipt of an expense line: a vault document at the mission's level."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    expense = models.ForeignKey(
        Expense, on_delete=models.PROTECT, related_name="receipts", editable=False
    )
    document = models.OneToOneField(
        "vault.Document", on_delete=models.PROTECT, related_name="+", editable=False
    )

    class Meta:
        verbose_name = _("receipt")
        ordering: ClassVar[list[str]] = ["created_at", "pk"]

    def __str__(self) -> str:
        return f"receipt {self.pk}"


BUDGET_LEVEL = {
    Payer.ST.value: ConfidentialityLevel.INTERNAL_ST,
    Payer.LIRMM.value: ConfidentialityLevel.INTERNAL_LIRMM,
}


class TravelBudget(TimeStampedModel):
    """The yearly (calendar year) travel budget of one payer; amount sealed at the
    payer's level (slot travel.TravelBudget.amount)."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    thesis = models.ForeignKey(
        "accounts.Thesis", on_delete=models.PROTECT, related_name="+", editable=False
    )
    payer = models.CharField(_("payer"), max_length=8, choices=Payer.choices, editable=False)
    year = models.PositiveSmallIntegerField(_("year"), editable=False)
    level = models.CharField(max_length=20, choices=ConfidentialityLevel.choices, editable=False)

    class Meta:
        verbose_name = _("travel budget")
        ordering: ClassVar[list[str]] = ["year", "payer"]
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.UniqueConstraint(
                fields=["thesis", "payer", "year"], name="travel_budget_unique_year"
            ),
            models.CheckConstraint(
                condition=Q(payer=Payer.ST, level=BUDGET_LEVEL[Payer.ST.value])
                | Q(payer=Payer.LIRMM, level=BUDGET_LEVEL[Payer.LIRMM.value]),
                name="travel_budget_payer_level",
            ),
        ]

    def __str__(self) -> str:
        return f"travel budget {self.pk}"


# --------------------------------------------------------------------------- notifications


class TravelNotification(TimeStampedModel):
    """One e-mail (a link only) to one recipient: a reminder in one window, or an
    event. Unique per (kind, target, window, recipient): sent once."""

    Kind = NotificationKind
    Status = NotificationStatus

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    thesis = models.ForeignKey(
        "accounts.Thesis", on_delete=models.PROTECT, related_name="+", editable=False
    )
    kind = models.CharField(max_length=20, choices=NotificationKind.choices, editable=False)
    target_id = models.UUIDField(editable=False)  # the event (uuid5 of its parts)
    mission = models.ForeignKey(  # the page the e-mail links to; none: the settings
        Mission, on_delete=models.PROTECT, null=True, blank=True, related_name="+", editable=False
    )
    window = models.CharField(max_length=12, choices=Window.choices, blank=True, default="")
    recipient = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+", editable=False
    )
    status = models.CharField(
        max_length=10, choices=NotificationStatus.choices, default=NotificationStatus.PENDING
    )
    attempts = models.PositiveSmallIntegerField(default=0)
    sent_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = _("travel notification")
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.UniqueConstraint(
                fields=["kind", "target_id", "window", "recipient"], name="travel_notification_once"
            ),
            models.CheckConstraint(
                condition=Q(kind__in=NotificationKind.values)
                & Q(window__in=Window.values)
                & Q(status__in=NotificationStatus.values),
                name="travel_notification_choices",
            ),
        ]

    def __str__(self) -> str:
        return f"travel notification {self.pk}"
