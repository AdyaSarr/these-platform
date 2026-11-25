"""Portfolio (M9a): the trainings of a thesis, the date of the next re-registration,
and the reminders sent before it.

No free text in these tables: the title, provider and notes of a training are
sealed by the vault (vault.SealedText, slots portfolio.Training.*), at the level of
the training. What counts, sorts and filters is in clear: category, hours, dates,
level, ADUM state (docs/modules/M09-portfolio.md).
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import ClassVar

from django.conf import settings
from django.db import models
from django.db.models import F, Q
from django.utils.translation import gettext_lazy as _
from django.utils.translation import pgettext_lazy

from apps.accounts.permissions import Action, ReadableQuerySet
from apps.core.confidentiality import ConfidentialityLevel
from apps.core.models import TimeStampedModel

HOURS_MIN = Decimal("0.5")
HOURS_MAX = Decimal("300")


class TrainingCategory(models.TextChoices):
    """Provisional list of the sheet: to be confirmed by the doctoral school (ED2)."""

    ED = "ED", _("Doctoral school training")
    SCIENTIFIC = "SCIENTIFIC", _("Scientific training")
    TRANSVERSAL = "TRANSVERSAL", _("Transversal training")
    INTEGRITY = "INTEGRITY", _("Research ethics and integrity")
    OTHER = "OTHER", _("Other training")


class AdumStatus(models.TextChoices):
    """What the student declares: the platform never knows whether ADUM validated."""

    TO_ENTER = "TO_ENTER", _("To enter in ADUM")
    ENTERED = "ENTERED", _("Entered in ADUM")


class TextField(models.TextChoices):
    """The sealed texts of a training (slots portfolio.Training.<value>)."""

    TITLE = "title", _("Title")
    PROVIDER = "provider", _("Provider")
    NOTES = "notes", _("Notes")


TEXT_FIELDS = tuple(TextField)


class TrainingQuerySet(ReadableQuerySet["Training"]):
    read_action = Action.TRAINING_READ


class Training(TimeStampedModel):
    """A training followed (or to come) by the student. Its texts are sealed."""

    Category = TrainingCategory

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    thesis = models.ForeignKey(
        "accounts.Thesis", on_delete=models.PROTECT, related_name="trainings", editable=False
    )
    category = models.CharField(_("category"), max_length=20, choices=TrainingCategory.choices)
    hours = models.DecimalField(_("hours"), max_digits=4, decimal_places=1)
    start_date = models.DateField(_("start date"))
    end_date = models.DateField(_("end date"))
    level = models.CharField(
        _("confidentiality"),
        max_length=20,
        choices=ConfidentialityLevel.choices,
        default=ConfidentialityLevel.PUBLIC,
        editable=False,
    )
    certificate = models.ForeignKey(
        "vault.Document",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="+",
        verbose_name=_("certificate"),
    )
    adum_status = models.CharField(
        _("ADUM"),
        max_length=10,
        choices=AdumStatus.choices,
        default=AdumStatus.TO_ENTER,
        editable=False,
    )

    objects = TrainingQuerySet.as_manager()

    class Meta:
        verbose_name = _("training")
        verbose_name_plural = _("trainings")
        ordering: ClassVar[list[str]] = ["-end_date", "-start_date", "created_at"]
        indexes: ClassVar[list[models.Index]] = [models.Index(fields=["thesis", "end_date"])]
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.CheckConstraint(
                condition=Q(category__in=TrainingCategory.values),
                name="portfolio_training_category",
            ),
            models.CheckConstraint(
                condition=Q(hours__gte=HOURS_MIN, hours__lte=HOURS_MAX),
                name="portfolio_training_hours",
            ),
            models.CheckConstraint(
                condition=Q(start_date__lte=F("end_date")), name="portfolio_training_dates"
            ),
            models.CheckConstraint(
                condition=Q(level__in=ConfidentialityLevel.values),
                name="portfolio_training_level",
            ),
            models.CheckConstraint(
                condition=Q(adum_status__in=AdumStatus.values), name="portfolio_training_adum"
            ),
        ]

    def __str__(self) -> str:
        return f"training {self.pk}"


class PortfolioPolicy(TimeStampedModel):
    """One row per thesis: the date of the next re-registration, typed by the
    student (the calendar of the university is not archived: ED9)."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    thesis = models.OneToOneField(
        "accounts.Thesis", on_delete=models.PROTECT, related_name="+", editable=False
    )
    reregistration_date = models.DateField(_("next re-registration"), null=True, blank=True)

    class Meta:
        verbose_name = _("portfolio settings")
        verbose_name_plural = _("portfolio settings")

    def __str__(self) -> str:
        return f"portfolio settings of thesis {self.thesis_id}"


class NotificationKind(models.TextChoices):
    REREGISTRATION = "REREGISTRATION", _("Re-registration reminder")


class Window(models.TextChoices):
    J30 = "J30", _("30 days before")
    J7 = "J7", _("7 days before")


class NotificationStatus(models.TextChoices):
    PENDING = "PENDING", pgettext_lazy("notification", "Pending")
    SENT = "SENT", _("Sent")
    FAILED = "FAILED", _("Failed")
    SKIPPED = "SKIPPED", _("Not sent: no longer concerned")
    DEFERRED = "DEFERRED", _("Waiting for a digest")
    DIGESTED = "DIGESTED", _("Sent in a digest")


class PortfolioNotification(TimeStampedModel):
    """One e-mail (a link only) to one recipient, in one window. Unique per (kind,
    target, window, recipient): sent once. ``target_id`` names the date reminded
    (uuid5 of the thesis and the date): a changed date is a new event."""

    Kind = NotificationKind
    Status = NotificationStatus

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    thesis = models.ForeignKey(
        "accounts.Thesis", on_delete=models.PROTECT, related_name="+", editable=False
    )
    kind = models.CharField(max_length=20, choices=NotificationKind.choices, editable=False)
    target_id = models.UUIDField(editable=False)
    due_on = models.DateField(editable=False)  # the date reminded, checked again at send time
    window = models.CharField(max_length=4, choices=Window.choices, editable=False)
    recipient = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+", editable=False
    )
    status = models.CharField(
        max_length=10, choices=NotificationStatus.choices, default=NotificationStatus.PENDING
    )
    attempts = models.PositiveSmallIntegerField(default=0)
    sent_at = models.DateTimeField(null=True, blank=True)
    # M11b: the digest that sent this row (DIGESTED), kept for the history.
    digest = models.ForeignKey(
        "activity.ActivityNotification",
        on_delete=models.SET_NULL,
        null=True,
        related_name="+",
        editable=False,
    )

    class Meta:
        verbose_name = _("portfolio notification")
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.UniqueConstraint(
                fields=["kind", "target_id", "window", "recipient"],
                name="portfolio_notification_once",
            ),
            models.CheckConstraint(
                condition=Q(kind__in=NotificationKind.values)
                & Q(window__in=Window.values)
                & Q(status__in=NotificationStatus.values),
                name="portfolio_notification_choices",
            ),
        ]

    def __str__(self) -> str:
        return f"portfolio notification {self.pk}"
