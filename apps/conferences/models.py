"""Conferences and submissions (M6).

The catalogue (venues, editions, deadlines) is public information, in clear text,
per thesis. Submissions hold no free text: title, abstract, external authors,
notes, approval comments and preprint URLs are sealed by the vault
(vault.SealedText, slots conferences.*), at the level of their submission.

ST publication approval (CIFRE): a StApproval names the exact DocumentVersion the
tutor reviewed; once decided it is history, never modified, never deleted.
"""

from __future__ import annotations

import uuid
from typing import Any, ClassVar, TypeVar

from django.conf import settings
from django.db import models
from django.db.models import F, Q
from django.utils.translation import gettext_lazy as _

from apps.core.confidentiality import ConfidentialityLevel
from apps.core.models import TimeStampedModel

MAX_REVIEW_DAYS = 180


class HistoryError(Exception):
    """A decided approval or a preprint is never modified or deleted."""


_M = TypeVar("_M", bound=models.Model)


class _HistoryQuerySet(models.QuerySet[_M]):
    def update(self, **kwargs: Any) -> int:
        raise HistoryError("a history is never updated in bulk")

    def delete(self) -> tuple[int, dict[str, int]]:
        raise HistoryError("a history is never deleted")

    def bulk_update(self, *args: Any, **kwargs: Any) -> int:
        raise HistoryError("a history is never updated in bulk")

    def _raw_delete(self, *args: Any, **kwargs: Any) -> int:
        raise HistoryError("a history is never deleted")


def _in(field: str, values: list[str], name: str) -> models.CheckConstraint:
    return models.CheckConstraint(condition=Q(**{f"{field}__in": values}), name=name)


# --------------------------------------------------------------------------- catalogue


class VenueKind(models.TextChoices):
    CONFERENCE = "CONFERENCE", _("Conference")
    WORKSHOP = "WORKSHOP", _("Workshop")
    JOURNAL = "JOURNAL", _("Journal")


class Venue(TimeStampedModel):
    Kind = VenueKind

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    thesis = models.ForeignKey(
        "accounts.Thesis", on_delete=models.PROTECT, related_name="venues", editable=False
    )
    kind = models.CharField(_("type"), max_length=20, choices=VenueKind.choices)
    acronym = models.CharField(_("acronym"), max_length=32)
    name = models.CharField(_("name"), max_length=200)
    url = models.CharField(_("website"), max_length=300, blank=True, default="")

    class Meta:
        verbose_name = _("venue")
        ordering: ClassVar[list[str]] = ["acronym", "kind"]
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.UniqueConstraint(
                fields=["thesis", "kind", "acronym"], name="conferences_venue_once"
            ),
            _in("kind", VenueKind.values, "conferences_venue_kind"),
            models.CheckConstraint(
                condition=Q(url="") | Q(url__startswith="https://"),
                name="conferences_venue_https",
            ),
            models.CheckConstraint(condition=~Q(acronym=""), name="conferences_venue_acronym"),
        ]

    def __str__(self) -> str:
        return self.acronym

    @property
    def is_journal(self) -> bool:
        return self.kind == VenueKind.JOURNAL


class Edition(TimeStampedModel):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    thesis = models.ForeignKey(
        "accounts.Thesis", on_delete=models.PROTECT, related_name="+", editable=False
    )
    venue = models.ForeignKey(
        Venue, on_delete=models.PROTECT, related_name="editions", editable=False
    )
    year = models.PositiveSmallIntegerField(_("year"))
    location = models.CharField(_("place"), max_length=200, blank=True, default="")
    event_start = models.DateField(_("first day"), null=True, blank=True)
    event_end = models.DateField(_("last day"), null=True, blank=True)

    class Meta:
        verbose_name = _("edition")
        ordering: ClassVar[list[str]] = ["venue", "year"]
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.UniqueConstraint(fields=["venue", "year"], name="conferences_edition_once"),
            models.CheckConstraint(
                condition=Q(year__gte=2000, year__lte=2100), name="conferences_edition_year"
            ),
            models.CheckConstraint(
                condition=Q(event_start__isnull=True)
                | Q(event_end__isnull=True)
                | Q(event_start__lte=F("event_end")),
                name="conferences_edition_dates",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.venue_id} {self.year}"


class DeadlineKind(models.TextChoices):
    ABSTRACT = "ABSTRACT", _("Abstract")
    PAPER = "PAPER", _("Paper")
    REBUTTAL = "REBUTTAL", _("Rebuttal")
    NOTIFICATION = "NOTIFICATION", _("Notification")
    CAMERA_READY = "CAMERA_READY", _("Final version")
    REGISTRATION = "REGISTRATION", _("Registration")


class Deadline(TimeStampedModel):
    """A deadline of an edition: an instant in UTC and the zone it was given in
    ("AoE" or an IANA name, core.timezones)."""

    Kind = DeadlineKind

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    thesis = models.ForeignKey(
        "accounts.Thesis", on_delete=models.PROTECT, related_name="+", editable=False
    )
    edition = models.ForeignKey(
        Edition, on_delete=models.PROTECT, related_name="deadlines", editable=False
    )
    kind = models.CharField(_("type"), max_length=20, choices=DeadlineKind.choices)
    due_at = models.DateTimeField(_("due (UTC)"))
    tz_name = models.CharField(_("time zone"), max_length=64)
    cancelled_at = models.DateTimeField(null=True, blank=True, editable=False)

    class Meta:
        verbose_name = _("deadline")
        ordering: ClassVar[list[str]] = ["due_at", "pk"]
        indexes: ClassVar[list[models.Index]] = [
            models.Index(fields=["thesis", "due_at"], name="conferences_deadline_due")
        ]
        constraints: ClassVar[list[models.BaseConstraint]] = [
            _in("kind", DeadlineKind.values, "conferences_deadline_kind"),
            models.CheckConstraint(condition=~Q(tz_name=""), name="conferences_deadline_tz"),
        ]

    def __str__(self) -> str:
        return f"deadline {self.pk}"


class PublicationPolicy(TimeStampedModel):
    """The ST review period of a thesis, set by the industrial tutor only."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    thesis = models.OneToOneField(
        "accounts.Thesis",
        on_delete=models.PROTECT,
        related_name="publication_policy",
        editable=False,
    )
    st_review_days = models.PositiveSmallIntegerField(_("ST review period (days)"), default=30)

    class Meta:
        verbose_name = _("publication policy")
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.CheckConstraint(
                condition=Q(st_review_days__lte=MAX_REVIEW_DAYS),
                name="conferences_policy_days",
            )
        ]

    def __str__(self) -> str:
        return f"policy {self.pk}"


# --------------------------------------------------------------------------- submissions


class SubmissionStatus(models.TextChoices):
    PREPARING = "PREPARING", _("In preparation")
    ST_REQUESTED = "ST_REQUESTED", _("ST approval requested")
    ST_GRANTED = "ST_GRANTED", _("ST approval granted")
    SUBMITTED = "SUBMITTED", _("Submitted")
    ACCEPTED = "ACCEPTED", _("Accepted")
    REJECTED = "REJECTED", _("Rejected")
    WITHDRAWN = "WITHDRAWN", _("Withdrawn")
    PUBLISHED = "PUBLISHED", _("Published")


TERMINAL = frozenset(
    {SubmissionStatus.REJECTED, SubmissionStatus.WITHDRAWN, SubmissionStatus.PUBLISHED}
)
SUBMITTED_ONCE = ["SUBMITTED", "ACCEPTED", "REJECTED", "PUBLISHED"]
# A submission on ST work is read by the tutor who approves it.
ST_LEVELS = ["PUBLIC", "INTERNAL_ST", "CONFIDENTIAL"]


class Submission(TimeStampedModel):
    """A paper aimed at an edition (conference, workshop) or a journal."""

    Status = SubmissionStatus

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    thesis = models.ForeignKey(
        "accounts.Thesis", on_delete=models.PROTECT, related_name="submissions", editable=False
    )
    venue = models.ForeignKey(Venue, on_delete=models.PROTECT, related_name="submissions")
    edition = models.ForeignKey(
        Edition, on_delete=models.PROTECT, null=True, blank=True, related_name="submissions"
    )
    objective = models.ForeignKey(
        "objectives.Objective",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="submissions",
    )
    level = models.CharField(
        _("confidentiality"),
        max_length=20,
        choices=ConfidentialityLevel.choices,
        default=ConfidentialityLevel.PUBLIC,
    )
    status = models.CharField(
        _("status"), max_length=20, choices=SubmissionStatus.choices, default="PREPARING"
    )
    st_work = models.BooleanField(_("concerns ST work"), default=True)
    submitted_version = models.ForeignKey(
        "vault.DocumentVersion", on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    submitted_at = models.DateTimeField(null=True, blank=True, editable=False)
    published_version = models.ForeignKey(
        "vault.DocumentVersion", on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    published_at = models.DateTimeField(null=True, blank=True, editable=False)
    resubmission_of = models.ForeignKey(
        "self", on_delete=models.PROTECT, null=True, blank=True, related_name="resubmissions"
    )

    class Meta:
        verbose_name = _("submission")
        ordering: ClassVar[list[str]] = ["created_at", "pk"]
        constraints: ClassVar[list[models.BaseConstraint]] = [
            _in("level", ConfidentialityLevel.values, "conferences_submission_level"),
            _in("status", SubmissionStatus.values, "conferences_submission_status"),
            models.CheckConstraint(
                condition=Q(st_work=False) | Q(level__in=ST_LEVELS),
                name="conferences_submission_st_level",
            ),
            models.CheckConstraint(
                condition=~Q(status__in=SUBMITTED_ONCE) | Q(submitted_at__isnull=False),
                name="conferences_submission_submitted_at",
            ),
            models.CheckConstraint(
                condition=(Q(status="PUBLISHED") & Q(published_at__isnull=False))
                | (~Q(status="PUBLISHED") & Q(published_at__isnull=True)),
                name="conferences_submission_published_at",
            ),
        ]

    def __str__(self) -> str:
        return f"submission {self.pk}"  # never the title: it is sealed

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL


class SubmissionAuthor(TimeStampedModel):
    """An internal co-author (a member of the thesis); external ones are a sealed text."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    submission = models.ForeignKey(
        Submission, on_delete=models.PROTECT, related_name="authors", editable=False
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+", editable=False
    )

    class Meta:
        verbose_name = _("internal co-author")
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.UniqueConstraint(fields=["submission", "user"], name="conferences_author_once")
        ]

    def __str__(self) -> str:
        return f"author {self.pk}"


class SubmissionDocument(TimeStampedModel):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    submission = models.ForeignKey(
        Submission, on_delete=models.PROTECT, related_name="document_links", editable=False
    )
    document = models.ForeignKey(
        "vault.Document", on_delete=models.PROTECT, related_name="+", editable=False
    )

    class Meta:
        verbose_name = _("submission document")
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.UniqueConstraint(
                fields=["submission", "document"], name="conferences_document_once"
            )
        ]

    def __str__(self) -> str:
        return f"submission document {self.pk}"


# --------------------------------------------------------------------------- ST approval


class ApprovalPurpose(models.TextChoices):
    SUBMISSION = "SUBMISSION", _("Submission")
    CAMERA_READY = "CAMERA_READY", _("Final version")
    PREPRINT = "PREPRINT", _("Preprint")


class ApprovalDecision(models.TextChoices):
    PENDING = "PENDING", _("Pending")
    GRANTED = "GRANTED", _("Granted")
    REFUSED = "REFUSED", _("Refused")
    WITHDRAWN = "WITHDRAWN", _("Withdrawn")


class RefusalReason(models.TextChoices):
    PATENT_PENDING = "PATENT_PENDING", _("A patent must be filed first")
    CONFIDENTIAL_INFORMATION = "CONFIDENTIAL_INFORMATION", _("Confidential information")
    ST_RESULTS_NOT_CLEARED = "ST_RESULTS_NOT_CLEARED", _("ST results not cleared")
    CHANGES_REQUESTED = "CHANGES_REQUESTED", _("Changes requested")
    OTHER = "OTHER", _("Other")


class StApproval(TimeStampedModel):
    """A request for ST's publication approval of one exact document version.

    ``review_days`` and ``request_due`` are frozen when it is made. PENDING is
    decided once (granted, refused, withdrawn); a decided approval is history.
    """

    Purpose = ApprovalPurpose
    Decision = ApprovalDecision
    Reason = RefusalReason

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    thesis = models.ForeignKey(
        "accounts.Thesis", on_delete=models.PROTECT, related_name="+", editable=False
    )
    submission = models.ForeignKey(
        Submission, on_delete=models.PROTECT, related_name="approvals", editable=False
    )
    purpose = models.CharField(max_length=20, choices=ApprovalPurpose.choices, editable=False)
    version = models.ForeignKey(
        "vault.DocumentVersion", on_delete=models.PROTECT, related_name="+", editable=False
    )
    review_days = models.PositiveSmallIntegerField(editable=False)
    request_due = models.DateField(null=True, blank=True, editable=False)
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+", editable=False
    )
    decision = models.CharField(
        max_length=20, choices=ApprovalDecision.choices, default=ApprovalDecision.PENDING
    )
    reason = models.CharField(max_length=30, choices=RefusalReason.choices, blank=True, default="")
    decided_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    decided_at = models.DateTimeField(null=True, blank=True)

    objects = _HistoryQuerySet.as_manager()

    class Meta:
        verbose_name = _("ST publication approval")
        ordering: ClassVar[list[str]] = ["created_at", "pk"]
        constraints: ClassVar[list[models.BaseConstraint]] = [
            _in("purpose", ApprovalPurpose.values, "conferences_approval_purpose"),
            _in("decision", ApprovalDecision.values, "conferences_approval_decision"),
            models.CheckConstraint(
                condition=Q(reason="") | Q(reason__in=RefusalReason.values),
                name="conferences_approval_reason_known",
            ),
            models.CheckConstraint(
                condition=Q(review_days__lte=MAX_REVIEW_DAYS), name="conferences_approval_days"
            ),
            models.UniqueConstraint(
                fields=["submission", "purpose"],
                condition=Q(decision="PENDING"),
                name="conferences_approval_one_pending",
            ),
            models.CheckConstraint(
                condition=(Q(decision="PENDING") & Q(decided_at__isnull=True))
                | (~Q(decision="PENDING") & Q(decided_at__isnull=False)),
                name="conferences_approval_decided_at",
            ),
            models.CheckConstraint(
                condition=(Q(decision__in=["GRANTED", "REFUSED"]) & Q(decided_by__isnull=False))
                | (~Q(decision__in=["GRANTED", "REFUSED"]) & Q(decided_by__isnull=True)),
                name="conferences_approval_decided_by",
            ),
            models.CheckConstraint(
                condition=Q(decided_by__isnull=True) | ~Q(decided_by=F("requested_by")),
                name="conferences_approval_not_by_requester",
            ),
            models.CheckConstraint(
                condition=(Q(decision="REFUSED") & ~Q(reason=""))
                | (~Q(decision="REFUSED") & Q(reason="")),
                name="conferences_approval_reason_iff_refused",
            ),
        ]

    def save(self, *args: Any, **kwargs: Any) -> None:
        if not self._state.adding:
            stored = StApproval.objects.filter(pk=self.pk).values_list("decision", flat=True)
            if stored.first() != ApprovalDecision.PENDING:
                raise HistoryError("a decided approval is never modified")
        super().save(*args, **kwargs)

    def delete(self, *args: Any, **kwargs: Any) -> tuple[int, dict[str, int]]:
        raise HistoryError("an approval is never deleted")

    def __str__(self) -> str:
        return f"approval {self.pk}"


class PreprintPlatform(models.TextChoices):
    EPRINT = "EPRINT", _("IACR ePrint")
    ARXIV = "ARXIV", _("arXiv")
    HAL = "HAL", _("HAL")
    OTHER = "OTHER", _("Other")


class Preprint(TimeStampedModel):
    """A public preprint of a submission; its URL is sealed (conferences.Preprint.url)."""

    Platform = PreprintPlatform

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    thesis = models.ForeignKey(
        "accounts.Thesis", on_delete=models.PROTECT, related_name="+", editable=False
    )
    submission = models.ForeignKey(
        Submission, on_delete=models.PROTECT, related_name="preprints", editable=False
    )
    platform = models.CharField(max_length=10, choices=PreprintPlatform.choices, editable=False)
    posted_on = models.DateField(editable=False)
    version = models.ForeignKey(
        "vault.DocumentVersion",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="+",
        editable=False,
    )

    objects = _HistoryQuerySet.as_manager()

    class Meta:
        verbose_name = _("preprint")
        ordering: ClassVar[list[str]] = ["posted_on", "pk"]
        constraints: ClassVar[list[models.BaseConstraint]] = [
            _in("platform", PreprintPlatform.values, "conferences_preprint_platform")
        ]

    def save(self, *args: Any, **kwargs: Any) -> None:
        if not self._state.adding:
            raise HistoryError("a preprint is never modified")
        super().save(*args, **kwargs)

    def delete(self, *args: Any, **kwargs: Any) -> tuple[int, dict[str, int]]:
        raise HistoryError("a preprint is never deleted")

    def __str__(self) -> str:
        return f"preprint {self.pk}"


# --------------------------------------------------------------------------- notifications


class NotificationKind(models.TextChoices):
    DEADLINE = "DEADLINE", _("Deadline reminder")
    REQUEST_DUE = "REQUEST_DUE", _("ST approval request due")
    APPROVAL_PENDING = "APPROVAL_PENDING", _("ST approval awaiting a decision")
    APPROVAL_REQUESTED = "APPROVAL_REQUESTED", _("ST approval requested")
    APPROVAL_DECIDED = "APPROVAL_DECIDED", _("ST approval decided")
    POLICY_CHANGED = "POLICY_CHANGED", _("ST review period changed")


class Window(models.TextChoices):
    NONE = "", _("Event")
    J30 = "J30", "J-30"
    J14 = "J14", "J-14"
    J7 = "J7", "J-7"
    J1 = "J1", "J-1"


class NotificationStatus(models.TextChoices):
    PENDING = "PENDING", _("Pending")
    SENT = "SENT", _("Sent")
    FAILED = "FAILED", _("Failed")
    SKIPPED = "SKIPPED", _("Not sent: no longer concerned or due")
    DEFERRED = "DEFERRED", _("Waiting for a digest")
    DIGESTED = "DIGESTED", _("Sent in a digest")


class ConferenceNotification(TimeStampedModel):
    """One e-mail (a link only) to one recipient: a reminder of a deadline in one
    window (target = deadline, or the approval's event), or an event."""

    Kind = NotificationKind
    Status = NotificationStatus

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    thesis = models.ForeignKey(
        "accounts.Thesis", on_delete=models.PROTECT, related_name="+", editable=False
    )
    kind = models.CharField(max_length=20, choices=NotificationKind.choices, editable=False)
    target_id = models.UUIDField(editable=False)
    # The page the e-mail links to: an edition (DEADLINE) or a submission (REQUEST_DUE,
    # APPROVAL_PENDING); target_id names the event (deadline and its instant, etc.).
    link_id = models.UUIDField(null=True, blank=True, editable=False)
    window = models.CharField(max_length=4, choices=Window.choices, blank=True, default="")
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
        verbose_name = _("conference notification")
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.UniqueConstraint(
                fields=["kind", "target_id", "window", "recipient"],
                name="conferences_notification_once",
            ),
            _in("kind", NotificationKind.values, "conferences_notification_kind"),
            _in("window", Window.values, "conferences_notification_window"),
            _in("status", NotificationStatus.values, "conferences_notification_status"),
        ]

    def __str__(self) -> str:
        return f"conference notification {self.pk}"
