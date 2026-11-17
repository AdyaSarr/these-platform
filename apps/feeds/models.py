"""The personal iCal feed of one person on one thesis (M12).

The token is never stored: only its SHA-256 fingerprint (vault.crypto.hash_token),
as for invitations. At most one live feed per (person, thesis).
"""

from __future__ import annotations

import uuid
from typing import ClassVar

from django.conf import settings
from django.db import models
from django.db.models import Q

from apps.core.models import TimeStampedModel


class CalendarFeed(TimeStampedModel):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+", editable=False
    )
    thesis = models.ForeignKey(
        "accounts.Thesis", on_delete=models.PROTECT, related_name="+", editable=False
    )
    token_hash = models.CharField(max_length=64, unique=True, editable=False)
    expires_on = models.DateField(editable=False)
    revoked_at = models.DateTimeField(null=True, editable=False)
    last_used_on = models.DateField(null=True, editable=False)  # the day only

    class Meta:
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.UniqueConstraint(
                fields=["user", "thesis"],
                condition=Q(revoked_at__isnull=True),
                name="feeds_one_live_feed",
            ),
        ]

    def __str__(self) -> str:
        return f"calendar feed {self.pk}"  # never the token nor its fingerprint
