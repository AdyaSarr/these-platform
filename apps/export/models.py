"""Models of the student's export (M13b, docs/modules/M13-fin-de-these.md).

- ExportRecord: one export started (the hourly limit, the history on the page);
  counts only, never a title.
- StExportConsent: the industrial tutor's agreement to let INTERNAL_ST into the
  exports for 30 days, revocable.
- ExportNotification: one alert e-mail (a link only), sent once by
  core.delivery.deliver_once, its checks at send time (notifications.py).
"""

from __future__ import annotations

import uuid
from typing import ClassVar

from django.conf import settings
from django.db import models
from django.db.models import Q
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.core.models import TimeStampedModel


class ExportRecord(TimeStampedModel):
    class Outcome(models.TextChoices):
        STARTED = "STARTED", _("Started")
        COMPLETE = "COMPLETE", _("Complete")
        INCOMPLETE = "INCOMPLETE", _("Interrupted")

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    thesis = models.ForeignKey("accounts.Thesis", on_delete=models.PROTECT, related_name="+")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    started_at = models.DateTimeField(default=timezone.now, editable=False)
    finished_at = models.DateTimeField(null=True, blank=True)
    outcome = models.CharField(max_length=10, choices=Outcome.choices, default=Outcome.STARTED)
    reason = models.CharField(max_length=20, blank=True)  # a code: blob_missing, client_gone...
    all_versions = models.BooleanField(default=False)
    consent = models.ForeignKey(
        "StExportConsent", on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )

    class Meta:
        indexes: ClassVar[list[models.Index]] = [
            models.Index(fields=["thesis", "user", "started_at"], name="export_record_recent"),
        ]

    def __str__(self) -> str:
        return f"export {self.pk}"


class StExportConsent(TimeStampedModel):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    thesis = models.ForeignKey("accounts.Thesis", on_delete=models.PROTECT, related_name="+")
    granted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    valid_until = models.DateTimeField()
    revoked_at = models.DateTimeField(null=True, blank=True)
    revoked_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True,
        related_name="+",
    )  # fmt: skip

    class Meta:
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.CheckConstraint(
                condition=Q(revoked_at__isnull=True) | Q(revoked_by__isnull=False),
                name="export_consent_revoked_by_someone",
            ),
        ]

    def __str__(self) -> str:
        return f"ST export consent {self.pk}"

    def is_valid(self, at: object = None) -> bool:
        now = at or timezone.now()
        return self.revoked_at is None and now < self.valid_until  # type: ignore[operator]


class ExportNotification(TimeStampedModel):
    class Kind(models.TextChoices):
        EXPORTED = "EXPORTED", _("Export of your data")
        ST_CONSENT_GRANTED = "ST_CONSENT_GRANTED", _("ST agreement to the export")

    class Status(models.TextChoices):
        PENDING = "PENDING", _("Pending")
        SENT = "SENT", _("Sent")
        FAILED = "FAILED", _("Failed")
        SKIPPED = "SKIPPED", _("Not sent: no longer concerned or due")

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    thesis = models.ForeignKey("accounts.Thesis", on_delete=models.PROTECT, related_name="+")
    recipient = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    kind = models.CharField(max_length=20, choices=Kind.choices)
    export = models.ForeignKey(
        ExportRecord, on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    consent = models.ForeignKey(
        StExportConsent, on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.PENDING)
    attempts = models.PositiveSmallIntegerField(default=0)
    sent_at = models.DateTimeField(null=True, blank=True)

    def __str__(self) -> str:
        return f"export notification {self.pk}"
