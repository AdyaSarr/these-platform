"""Portfolio (M9a, M9b): the trainings of a thesis, the date of the next
re-registration and its reminders (M9a); the publication details of the M6
submissions, teaching, outreach, the student's skills and professional project
(M9b).

No free text in these tables: every text is sealed by the vault (vault.SealedText,
slots portfolio.*): a training's, a teaching's, an outreach action's at their own
level; the publication details' at their SUBMISSION's level (no level of their own,
never copied); the skills' and the professional project's at the fixed level
PERSONAL_LEVEL, read by the student alone (the actions decide it). What counts,
sorts and filters is in clear: category, type, hours, dates, level, ADUM state
(docs/modules/M09-portfolio.md).
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


# =========================================================================== M9b


class AdumCategory(models.TextChoices):
    """Provisional list of productions: to be confirmed by the doctoral school (ED7)."""

    JOURNAL_ARTICLE = "JOURNAL_ARTICLE", _("Journal article")
    CONFERENCE_PAPER = "CONFERENCE_PAPER", _("Conference paper with proceedings")
    ORAL_COMMUNICATION = "ORAL_COMMUNICATION", _("Oral communication")
    POSTER = "POSTER", _("Poster")
    INVITED_TALK = "INVITED_TALK", _("Invited talk")
    SEMINAR = "SEMINAR", _("Seminar")
    OTHER = "OTHER", _("Other production")


# The ADUM summary's « publications »; every other category is a « communication ».
PUBLICATION_CATEGORIES = frozenset({AdumCategory.JOURNAL_ARTICLE, AdumCategory.CONFERENCE_PAPER})
DETAILS_TEXTS = ("doi", "hal_id", "volume", "pages", "bibtex_key")


class PublicationDetailsQuerySet(ReadableQuerySet["PublicationDetails"]):
    """Readable as their submission is: SUBMISSION_READ and the submission's level."""

    read_action = Action.SUBMISSION_READ
    level_field = "submission__level"


class PublicationDetails(TimeStampedModel):
    """What ADUM and a bibliography ask of a submission of M6 (one row at most). Its
    texts are sealed at the submission's level; it has no level of its own."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    thesis = models.ForeignKey(
        "accounts.Thesis", on_delete=models.PROTECT, related_name="+", editable=False
    )
    submission = models.OneToOneField(
        "conferences.Submission",
        on_delete=models.PROTECT,
        related_name="publication_details",
        editable=False,
    )
    adum_category = models.CharField(
        _("ADUM category"), max_length=20, choices=AdumCategory.choices
    )
    presented_on = models.DateField(_("presented on"), null=True, blank=True)

    objects = PublicationDetailsQuerySet.as_manager()

    class Meta:
        verbose_name = _("publication details")
        verbose_name_plural = _("publication details")
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.CheckConstraint(
                condition=Q(adum_category__in=AdumCategory.values),
                name="portfolio_details_category",
            ),
        ]

    def __str__(self) -> str:
        return f"publication details {self.pk}"

    @property
    def level(self) -> str:
        """The submission's: read by sees() and the export, never stored here."""
        return str(self.submission.level)


class TeachingKind(models.TextChoices):
    """Provisional list: to be confirmed by the doctoral school (ED6)."""

    CM = "CM", _("Lecture (CM)")
    TD = "TD", _("Tutorial (TD)")
    TP = "TP", _("Practical work (TP)")
    SUPERVISION = "SUPERVISION", _("Student supervision")
    OTHER = "OTHER", _("Other teaching")


TEACHING_TEXTS = ("course", "institution", "notes")


class TeachingQuerySet(ReadableQuerySet["Teaching"]):
    read_action = Action.TEACHING_READ


class Teaching(TimeStampedModel):
    """A course given by the student. Its texts are sealed at its level."""

    Kind = TeachingKind

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    thesis = models.ForeignKey(
        "accounts.Thesis", on_delete=models.PROTECT, related_name="+", editable=False
    )
    kind = models.CharField(_("type"), max_length=20, choices=TeachingKind.choices)
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

    objects = TeachingQuerySet.as_manager()

    class Meta:
        verbose_name = _("teaching")
        verbose_name_plural = _("teaching")
        ordering: ClassVar[list[str]] = ["-end_date", "-start_date", "created_at"]
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.CheckConstraint(
                condition=Q(kind__in=TeachingKind.values)
                & Q(level__in=ConfidentialityLevel.values)
                & Q(hours__gte=HOURS_MIN, hours__lte=HOURS_MAX)
                & Q(start_date__lte=F("end_date")),
                name="portfolio_teaching_values",
            ),
        ]

    def __str__(self) -> str:
        return f"teaching {self.pk}"


class OutreachKind(models.TextChoices):
    """Provisional list: to be confirmed by the doctoral school (ED6)."""

    SCIENCE_FESTIVAL = "SCIENCE_FESTIVAL", _("Science festival")
    SCHOOL = "SCHOOL", _("School visit")
    PUBLIC_TALK = "PUBLIC_TALK", _("Talk for the general public")
    PRESS = "PRESS", _("Press, radio, video")
    ARTICLE = "ARTICLE", _("Popular article")
    OTHER = "OTHER", _("Other outreach")


OUTREACH_TEXTS = ("title", "audience", "notes")


class OutreachQuerySet(ReadableQuerySet["Outreach"]):
    read_action = Action.OUTREACH_READ


class Outreach(TimeStampedModel):
    """An outreach action of the student. Its texts are sealed at its level."""

    Kind = OutreachKind

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    thesis = models.ForeignKey(
        "accounts.Thesis", on_delete=models.PROTECT, related_name="+", editable=False
    )
    kind = models.CharField(_("type"), max_length=20, choices=OutreachKind.choices)
    date = models.DateField(_("date"))
    level = models.CharField(
        _("confidentiality"),
        max_length=20,
        choices=ConfidentialityLevel.choices,
        default=ConfidentialityLevel.PUBLIC,
        editable=False,
    )

    objects = OutreachQuerySet.as_manager()

    class Meta:
        verbose_name = _("outreach action")
        verbose_name_plural = _("outreach actions")
        ordering: ClassVar[list[str]] = ["-date", "created_at"]
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.CheckConstraint(
                condition=Q(kind__in=OutreachKind.values)
                & Q(level__in=ConfidentialityLevel.values),
                name="portfolio_outreach_values",
            ),
        ]

    def __str__(self) -> str:
        return f"outreach {self.pk}"


# The student's notes (Q1): sealed at this fixed level, read by the student alone.
PERSONAL_LEVEL = ConfidentialityLevel.CONFIDENTIAL


class SkillCategory(models.TextChoices):
    """Provisional list: a framework may be imposed (ED8)."""

    SCIENTIFIC = "SCIENTIFIC", _("Scientific")
    TECHNICAL = "TECHNICAL", _("Technical")
    TRANSVERSAL = "TRANSVERSAL", _("Transversal")
    LANGUAGE = "LANGUAGE", _("Language")
    OTHER = "OTHER", _("Other skill")


SKILL_TEXTS = ("title", "evidence")


class SkillNoteQuerySet(ReadableQuerySet["SkillNote"]):
    read_action = Action.CAREER_NOTE_READ
    fixed_level = PERSONAL_LEVEL


class CareerNoteQuerySet(ReadableQuerySet["CareerNote"]):
    read_action = Action.CAREER_NOTE_READ
    fixed_level = PERSONAL_LEVEL


class SkillNote(TimeStampedModel):
    """A skill the student notes, with what shows it. The student alone."""

    Category = SkillCategory

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    thesis = models.ForeignKey(
        "accounts.Thesis", on_delete=models.PROTECT, related_name="+", editable=False
    )
    category = models.CharField(_("category"), max_length=20, choices=SkillCategory.choices)

    objects = SkillNoteQuerySet.as_manager()

    class Meta:
        verbose_name = _("skill")
        verbose_name_plural = _("skills")
        ordering: ClassVar[list[str]] = ["category", "created_at"]
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.CheckConstraint(
                condition=Q(category__in=SkillCategory.values), name="portfolio_skill_category"
            ),
        ]

    def __str__(self) -> str:
        return f"skill {self.pk}"

    @property
    def level(self) -> str:
        return str(PERSONAL_LEVEL.value)


class CareerNote(TimeStampedModel):
    """The professional project of the student, one per thesis. The student alone."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    thesis = models.OneToOneField(
        "accounts.Thesis", on_delete=models.PROTECT, related_name="+", editable=False
    )

    objects = CareerNoteQuerySet.as_manager()

    class Meta:
        verbose_name = _("professional project")
        verbose_name_plural = _("professional projects")

    def __str__(self) -> str:
        return f"professional project {self.pk}"

    @property
    def level(self) -> str:
        return str(PERSONAL_LEVEL.value)


# =========================================================================== M9a settings


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
