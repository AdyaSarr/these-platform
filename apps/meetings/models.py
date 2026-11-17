"""Meetings (M5a): meetings between internal members, attendees, minutes
acknowledgements, decisions, actions, linked documents, notifications.

No free text in these tables: title, agenda, minutes, location detail, video
link, decision texts and action titles are sealed by the vault (vault.SealedText,
slots meetings.*), at the level of their meeting. Nothing is ever deleted: a
meeting is cancelled, a decision withdrawn, an action dropped.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any, ClassVar, TypeVar

from django.conf import settings
from django.db import models
from django.db.models import Q
from django.utils.translation import gettext_lazy as _

from apps.core.confidentiality import ConfidentialityLevel
from apps.core.models import TimeStampedModel

MIN_DURATION, MAX_DURATION = 5, 600


class HistoryError(Exception):
    """An acknowledgement is never modified or deleted."""


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


class MeetingKind(models.TextChoices):
    ST_TUTOR = "ST_TUTOR", _("Meeting with the ST tutor")
    SUPERVISION = "SUPERVISION", _("Supervision meeting")
    TRIPARTITE = "TRIPARTITE", _("Tripartite meeting")
    CSI = "CSI", _("Individual monitoring committee (CSI)")
    PRESENTATION = "PRESENTATION", _("Presentation")
    OTHER = "OTHER", _("Other meeting")


class Venue(models.TextChoices):
    TEAM_SEMINAR = "TEAM_SEMINAR", _("Team seminar")
    ST_MEETING = "ST_MEETING", _("ST meeting")
    OTHER = "OTHER", _("Other")


class Location(models.TextChoices):
    ROUSSET = "ROUSSET", _("ST Rousset")
    MONTPELLIER = "MONTPELLIER", _("LIRMM Montpellier")
    VISIO = "VISIO", _("Video call")
    OTHER = "OTHER", _("Other place")


class MeetingStatus(models.TextChoices):
    SCHEDULED = "SCHEDULED", _("Scheduled")
    CANCELLED = "CANCELLED", _("Cancelled")


class Meeting(TimeStampedModel):
    """A meeting of a thesis; its texts are sealed (slots meetings.Meeting.*)."""

    Kind = MeetingKind
    Venue = Venue
    Location = Location
    Status = MeetingStatus

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    thesis = models.ForeignKey(
        "accounts.Thesis", on_delete=models.PROTECT, related_name="meetings", editable=False
    )
    kind = models.CharField(_("type"), max_length=20, choices=Kind.choices)
    presentation_venue = models.CharField(
        _("presentation venue"), max_length=20, choices=Venue.choices, blank=True, default=""
    )
    starts_at = models.DateTimeField(_("start"))
    duration_minutes = models.PositiveSmallIntegerField(_("duration (minutes)"))
    location = models.CharField(_("place"), max_length=20, choices=Location.choices)
    level = models.CharField(
        _("confidentiality"),
        max_length=20,
        choices=ConfidentialityLevel.choices,
        default=ConfidentialityLevel.PUBLIC,
    )
    status = models.CharField(
        _("status"), max_length=20, choices=Status.choices, default=Status.SCHEDULED
    )
    cancelled_at = models.DateTimeField(null=True, blank=True, editable=False)
    minutes_version = models.PositiveIntegerField(default=0, editable=False)
    minutes_updated_at = models.DateTimeField(null=True, blank=True, editable=False)
    minutes_updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        editable=False,
        related_name="+",
    )
    ics_sequence = models.PositiveIntegerField(default=0, editable=False)

    class Meta:
        verbose_name = _("meeting")
        verbose_name_plural = _("meetings")
        ordering: ClassVar[list[str]] = ["starts_at", "pk"]
        indexes: ClassVar[list[models.Index]] = [
            models.Index(fields=["thesis", "starts_at"], name="meetings_thesis_start")
        ]
        constraints: ClassVar[list[models.BaseConstraint]] = [
            _in("kind", MeetingKind.values, "meetings_meeting_kind"),
            _in("location", Location.values, "meetings_meeting_location"),
            _in("level", ConfidentialityLevel.values, "meetings_meeting_level"),
            _in("status", MeetingStatus.values, "meetings_meeting_status"),
            models.CheckConstraint(
                condition=(Q(kind="PRESENTATION") & Q(presentation_venue__in=Venue.values))
                | (~Q(kind="PRESENTATION") & Q(presentation_venue="")),
                name="meetings_meeting_venue_iff_presentation",
            ),
            models.CheckConstraint(
                condition=Q(duration_minutes__gte=MIN_DURATION, duration_minutes__lte=MAX_DURATION),
                name="meetings_meeting_duration",
            ),
            models.CheckConstraint(
                condition=(Q(status="CANCELLED") & Q(cancelled_at__isnull=False))
                | (Q(status="SCHEDULED") & Q(cancelled_at__isnull=True)),
                name="meetings_meeting_cancelled_iff_cancelled_at",
            ),
            models.CheckConstraint(
                condition=Q(minutes_version=0)
                | (Q(minutes_updated_at__isnull=False) & Q(minutes_updated_by__isnull=False)),
                name="meetings_meeting_minutes_dated",
            ),
        ]

    def __str__(self) -> str:
        return f"meeting {self.pk}"  # never the title: it is sealed

    @property
    def ends_at(self) -> dt.datetime:
        return self.starts_at + dt.timedelta(minutes=self.duration_minutes)

    @property
    def is_cancelled(self) -> bool:
        return self.status == self.Status.CANCELLED

    @property
    def is_csi(self) -> bool:
        """M5a: a CSI meeting is planned, but its minutes, decisions and actions wait for M5b."""
        return self.kind == self.Kind.CSI


class Attendee(TimeStampedModel):
    """A member invited to a meeting. The services check the membership (active,
    internal role, level readable); the row names the user, so that a change of
    role (a new Membership row) keeps the attendance."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    meeting = models.ForeignKey(
        Meeting, on_delete=models.PROTECT, related_name="attendees", editable=False
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+", editable=False
    )

    class Meta:
        verbose_name = _("attendee")
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.UniqueConstraint(fields=["meeting", "user"], name="meetings_attendee_once")
        ]

    def __str__(self) -> str:
        return f"attendee {self.pk}"


class AcknowledgementKind(models.TextChoices):
    READ = "READ", _("Read")
    APPROVED = "APPROVED", _("Approved")


class MinutesAcknowledgement(TimeStampedModel):
    """« Read » or « approved » by an attendee, for one version of the minutes.
    Append only: an edit of the minutes leaves the marks on the older version."""

    Kind = AcknowledgementKind

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    meeting = models.ForeignKey(
        Meeting, on_delete=models.PROTECT, related_name="acknowledgements", editable=False
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+", editable=False
    )
    minutes_version = models.PositiveIntegerField(editable=False)
    kind = models.CharField(max_length=10, choices=Kind.choices, editable=False)

    objects = _HistoryQuerySet.as_manager()

    class Meta:
        verbose_name = _("acknowledgement of minutes")
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.UniqueConstraint(
                fields=["meeting", "user", "minutes_version", "kind"],
                name="meetings_acknowledgement_once",
            ),
            models.CheckConstraint(
                condition=Q(minutes_version__gte=1), name="meetings_acknowledgement_version"
            ),
            _in("kind", AcknowledgementKind.values, "meetings_acknowledgement_kind"),
        ]

    def save(self, *args: Any, **kwargs: Any) -> None:
        if not self._state.adding:
            raise HistoryError("an acknowledgement is never modified")
        super().save(*args, **kwargs)

    def delete(self, *args: Any, **kwargs: Any) -> tuple[int, dict[str, int]]:
        raise HistoryError("an acknowledgement is never deleted")

    def __str__(self) -> str:
        return f"acknowledgement {self.pk}"


class Decision(TimeStampedModel):
    """A decision of a meeting; its text is sealed (meetings.Decision.text)."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    meeting = models.ForeignKey(
        Meeting, on_delete=models.PROTECT, related_name="decisions", editable=False
    )
    thesis = models.ForeignKey(
        "accounts.Thesis", on_delete=models.PROTECT, related_name="+", editable=False
    )
    withdrawn_at = models.DateTimeField(null=True, blank=True, editable=False)

    class Meta:
        verbose_name = _("decision")
        ordering: ClassVar[list[str]] = ["created_at", "pk"]

    def __str__(self) -> str:
        return f"decision {self.pk}"


class ActionStatus(models.TextChoices):
    OPEN = "OPEN", _("Open")
    DONE = "DONE", _("Done")
    DROPPED = "DROPPED", _("Dropped")


class ActionItem(TimeStampedModel):
    """An action decided in a meeting; its title is sealed (meetings.ActionItem.title)."""

    Status = ActionStatus

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    meeting = models.ForeignKey(
        Meeting, on_delete=models.PROTECT, related_name="actions", editable=False
    )
    thesis = models.ForeignKey(
        "accounts.Thesis", on_delete=models.PROTECT, related_name="+", editable=False
    )
    assignee = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    due_date = models.DateField(_("due date"))
    status = models.CharField(
        _("status"), max_length=10, choices=Status.choices, default=Status.OPEN
    )
    done_at = models.DateTimeField(null=True, blank=True, editable=False)
    objective = models.ForeignKey(
        "objectives.Objective",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="meeting_actions",
    )

    class Meta:
        verbose_name = _("action")
        ordering: ClassVar[list[str]] = ["due_date", "created_at", "pk"]
        constraints: ClassVar[list[models.BaseConstraint]] = [
            _in("status", ActionStatus.values, "meetings_action_status"),
            models.CheckConstraint(
                condition=(Q(status="DONE") & Q(done_at__isnull=False))
                | (~Q(status="DONE") & Q(done_at__isnull=True)),
                name="meetings_action_done_iff_done_at",
            ),
        ]

    def __str__(self) -> str:
        return f"action {self.pk}"


class MeetingDocument(TimeStampedModel):
    """A vault document linked to a meeting; linking never changes who may read it."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    meeting = models.ForeignKey(
        Meeting, on_delete=models.PROTECT, related_name="document_links", editable=False
    )
    document = models.ForeignKey(
        "vault.Document", on_delete=models.PROTECT, related_name="+", editable=False
    )

    class Meta:
        verbose_name = _("linked document")
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.UniqueConstraint(fields=["meeting", "document"], name="meetings_document_once")
        ]

    def __str__(self) -> str:
        return f"meeting document {self.pk}"


class NotificationKind(models.TextChoices):
    INVITED = "INVITED", _("Invitation")
    CHANGED = "CHANGED", _("Date or place changed")
    CANCELLED = "CANCELLED", _("Cancelled")
    MINUTES = "MINUTES", _("Minutes to read")
    REMINDER = "REMINDER", _("Reminder the day before")
    ACTION_OVERDUE = "ACTION_OVERDUE", _("Overdue action")


class NotificationStatus(models.TextChoices):
    PENDING = "PENDING", _("Pending")
    SENT = "SENT", _("Sent")
    FAILED = "FAILED", _("Failed")
    SKIPPED = "SKIPPED", _("Not sent: no longer concerned or due")
    DEFERRED = "DEFERRED", _("Waiting for a digest")
    DIGESTED = "DIGESTED", _("Sent in a digest")


class MeetingNotification(TimeStampedModel):
    """One e-mail (a link only) about a meeting, to one recipient, for one event.

    Same mechanism as admin_file.ItemNotification: queued after commit with its
    id only, the row locked when sending (sent once), a FAILED one queued again by
    the daily `meetings_reminders` run.
    """

    Kind = NotificationKind

    Status = NotificationStatus

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    meeting = models.ForeignKey(
        Meeting, on_delete=models.PROTECT, related_name="notifications", editable=False
    )
    kind = models.CharField(max_length=20, choices=Kind.choices, editable=False)
    # The event that caused it: a fresh uuid per change; for REMINDER and
    # ACTION_OVERDUE a uuid5 of (meeting, start) and (action, due date), computed
    # again at send time (reminders.reminder_event, reminders.overdue_event).
    event_id = models.UUIDField(editable=False)
    recipient = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+", editable=False
    )
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.PENDING)
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
        verbose_name = _("meeting notification")
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.UniqueConstraint(
                fields=["kind", "event_id", "recipient"], name="meetings_notification_once"
            ),
            _in("kind", NotificationKind.values, "meetings_notification_kind"),
            _in("status", NotificationStatus.values, "meetings_notification_status"),
        ]

    def __str__(self) -> str:
        return f"meeting notification {self.pk}"
