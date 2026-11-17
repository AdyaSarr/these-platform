"""Activity events, their reads, preferences and e-mails (M11a).

Random UUID keys everywhere (uuid4): the gap between two ids reveals nothing. An
event holds no title, no level and no free text: what it is called and who reads
it are read on its object, at display or send time (readers.py, labels.py).
"""

from __future__ import annotations

import uuid
from typing import ClassVar

from django.conf import settings
from django.db import models
from django.db.models import Q

from apps.core.models import TimeStampedModel

from .choices import (
    Category,
    EventKind,
    Frequency,
    NotificationKind,
    NotificationStatus,
    ObjectType,
)


class Event(TimeStampedModel):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    thesis = models.ForeignKey(
        "accounts.Thesis", on_delete=models.CASCADE, related_name="+", editable=False
    )
    kind = models.CharField(max_length=32, choices=EventKind.choices, editable=False)
    object_type = models.CharField(max_length=24, choices=ObjectType.choices, editable=False)
    object_id = models.UUIDField(editable=False)
    # None: written by the system (an overdue objective, M11b).
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        related_name="+",
        editable=False,
    )

    class Meta:
        indexes: ClassVar[list[models.Index]] = [
            models.Index(fields=["thesis", "-created_at"], name="activity_event_recent"),
            models.Index(fields=["object_type", "object_id"], name="activity_event_object"),
        ]

    def __str__(self) -> str:
        return f"event {self.pk}"  # never a title: there is none


class EventRead(TimeStampedModel):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    event = models.ForeignKey(Event, on_delete=models.CASCADE, related_name="reads")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="+")

    class Meta:
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.UniqueConstraint(fields=["event", "user"], name="activity_read_once"),
        ]


class NotificationPreference(TimeStampedModel):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="+")
    thesis = models.ForeignKey("accounts.Thesis", on_delete=models.CASCADE, related_name="+")
    category = models.CharField(max_length=16, choices=Category.choices)
    frequency = models.CharField(max_length=16, choices=Frequency.choices)

    class Meta:
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.UniqueConstraint(
                fields=["user", "thesis", "category"], name="activity_one_preference"
            ),
            # M11b (choices.allowed_frequencies): « feed only » for Informations
            # only, never « weekly » for Échéances.
            models.CheckConstraint(
                condition=Q(category=Category.INFO)
                | (Q(category=Category.ACTION) & ~Q(frequency=Frequency.FEED_ONLY))
                | (
                    Q(category=Category.DEADLINE)
                    & Q(frequency__in=[Frequency.IMMEDIATE, Frequency.DAILY])
                ),
                name="activity_preference_frequencies",
            ),
        ]


class ActivityNotification(TimeStampedModel):
    """One e-mail of the activity app: an element (EVENT) or a digest."""

    Status = NotificationStatus

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    thesis = models.ForeignKey(
        "accounts.Thesis", on_delete=models.CASCADE, related_name="+", editable=False
    )
    recipient = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+", editable=False
    )
    kind = models.CharField(max_length=16, choices=NotificationKind.choices, editable=False)
    event = models.ForeignKey(
        Event, on_delete=models.CASCADE, null=True, related_name="+", editable=False
    )
    cycle = models.PositiveIntegerField(null=True, editable=False)  # an M3 piece's cycle
    # M11b: the category of an element (EVENT); the digest that sent it (DIGESTED);
    # for a digest, whether it covers the feed's Informations of its period.
    category = models.CharField(max_length=16, choices=Category.choices, blank=True, editable=False)
    digest = models.ForeignKey(
        "self", on_delete=models.SET_NULL, null=True, related_name="+", editable=False
    )
    includes_info = models.BooleanField(default=False, editable=False)
    period_start = models.DateField(null=True, editable=False)  # a digest's first day (Paris)
    status = models.CharField(
        max_length=10, choices=NotificationStatus.choices, default=NotificationStatus.PENDING
    )
    attempts = models.PositiveSmallIntegerField(default=0, editable=False)
    sent_at = models.DateTimeField(null=True, editable=False)

    class Meta:
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.UniqueConstraint(
                fields=["event", "recipient"],
                condition=Q(kind=NotificationKind.EVENT),
                name="activity_notification_event_once",
            ),
            models.UniqueConstraint(
                fields=["thesis", "kind", "period_start", "recipient"],
                condition=~Q(kind=NotificationKind.EVENT),
                name="activity_notification_digest_once",
            ),
            # A weekly digest covers a week starting on a Monday (dates in Paris).
            models.CheckConstraint(
                condition=~Q(kind=NotificationKind.DIGEST_WEEKLY) | Q(period_start__iso_week_day=1),
                name="activity_weekly_digest_monday",
            ),
            models.CheckConstraint(
                condition=(Q(kind=NotificationKind.EVENT) & Q(event__isnull=False))
                | (~Q(kind=NotificationKind.EVENT) & Q(period_start__isnull=False)),
                name="activity_notification_shape",
            ),
        ]

    def __str__(self) -> str:
        return f"activity notification {self.pk}"
