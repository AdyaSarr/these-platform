"""End of the thesis (M13a, docs/modules/M13-fin-de-these.md).

The status and its dates are on ``accounts.Thesis`` (``defended_on``, ``closed_at``,
``access_until``); this app keeps the two-person closure requests and its own
e-mails. Nothing here holds a free text: reasons are codes from closed lists.
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


class ClosureReason(models.TextChoices):
    DEFENDED = "defended", _("The thesis was defended")
    STUDENT_WITHDREW = "student_withdrew", _("The PhD student stops the thesis")
    CIFRE_TERMINATED = "cifre_terminated", _("Early end of the CIFRE contract")
    OTHER = "other", _("Another reason")


ABANDON_REASONS = frozenset(
    {ClosureReason.STUDENT_WITHDREW, ClosureReason.CIFRE_TERMINATED, ClosureReason.OTHER}
)


class CancelReason(models.TextChoices):
    WRONG_DATE = "wrong_date", _("Wrong date")
    DECLARED_BY_MISTAKE = "declared_by_mistake", _("Declared by mistake")
    POSTPONED = "postponed", _("Defence postponed")


class ClosureRequest(TimeStampedModel):
    """A request to close the thesis, by a director or the PhD student, confirmed
    by the other party within 14 days. One open request per thesis (database)."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    thesis = models.ForeignKey(
        "accounts.Thesis", on_delete=models.PROTECT, related_name="closure_requests"
    )
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    # The side of the requester: PHD_STUDENT or DIRECTOR (the other one confirms).
    requester_role = models.CharField(max_length=32)
    reason = models.CharField(_("reason"), max_length=32, choices=ClosureReason.choices)
    expires_at = models.DateTimeField()
    confirmed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    confirmed_at = models.DateTimeField(null=True, blank=True)
    withdrawn_at = models.DateTimeField(null=True, blank=True)
    expired_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.UniqueConstraint(
                fields=["thesis"],
                condition=Q(
                    confirmed_at__isnull=True, withdrawn_at__isnull=True, expired_at__isnull=True
                ),
                name="lifecycle_one_open_request",
            ),
            models.CheckConstraint(
                condition=Q(requester_role__in=["PHD_STUDENT", "DIRECTOR"]),
                name="lifecycle_requester_side",
            ),
            models.CheckConstraint(
                condition=Q(reason__in=[r.value for r in ClosureReason]),
                name="lifecycle_known_reason",
            ),
        ]

    def __str__(self) -> str:
        return f"closure request {self.pk}"

    @property
    def is_open(self) -> bool:
        return (
            self.confirmed_at is None
            and self.withdrawn_at is None
            and self.expired_at is None
            and timezone.now() < self.expires_at
        )


class LifecycleNotification(TimeStampedModel):
    """One e-mail of the lifecycle (a link only), to one recipient: sent once by
    core.delivery.deliver_once, its checks at send time (notifications.py)."""

    class Kind(models.TextChoices):
        DEFENDED = "DEFENDED", _("Defence declared")
        DEFENSE_CANCELLED = "DEFENSE_CANCELLED", _("Defence declaration cancelled")
        CLOSURE_REQUESTED = "CLOSURE_REQUESTED", _("Closure requested")

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
    closure_request = models.ForeignKey(
        ClosureRequest, on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.PENDING)
    attempts = models.PositiveSmallIntegerField(default=0)
    sent_at = models.DateTimeField(null=True, blank=True)

    def __str__(self) -> str:
        return f"lifecycle notification {self.pk}"
