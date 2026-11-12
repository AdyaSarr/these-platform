"""Audit log (M1: append-only in code; M2 adds the hash chain and a PostgreSQL trigger)."""

from __future__ import annotations

from typing import Any, ClassVar, NoReturn

from django.conf import settings
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from .errors import AuditError


class AuditEventQuerySet(models.QuerySet["AuditEvent"]):
    def update(self, **kwargs: Any) -> NoReturn:
        raise AuditError("The audit log is append-only.")

    def delete(self) -> NoReturn:
        raise AuditError("The audit log is append-only.")


class AuditEvent(models.Model):
    """One security-relevant event. Field names follow docs/SECURITY.md.

    Not a TimeStampedModel: an append-only row has no ``updated_at``.
    """

    seq = models.BigAutoField(primary_key=True)
    timestamp = models.DateTimeField(_("timestamp"), default=timezone.now, editable=False)
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        related_name="+",
        editable=False,
    )
    thesis = models.ForeignKey(
        "accounts.Thesis", on_delete=models.PROTECT, null=True, related_name="+", editable=False
    )
    action = models.CharField(_("action"), max_length=64, editable=False)
    object_type = models.CharField(max_length=100, blank=True, editable=False)
    object_id = models.CharField(max_length=64, blank=True, editable=False)
    ip_hash = models.CharField(max_length=64, blank=True, editable=False)
    details_json = models.JSONField(default=dict, editable=False)

    objects: ClassVar[AuditEventQuerySet] = AuditEventQuerySet.as_manager()  # type: ignore[assignment]

    class Meta:
        verbose_name = _("audit event")
        verbose_name_plural = _("audit events")
        ordering: ClassVar[list[str]] = ["seq"]
        indexes: ClassVar[list[models.Index]] = [models.Index(fields=["thesis", "timestamp"])]

    def __str__(self) -> str:
        return f"#{self.seq} {self.action}"

    def save(self, *args: Any, **kwargs: Any) -> None:
        if not self._state.adding:
            raise AuditError("The audit log is append-only.")
        super().save(*args, **kwargs)

    def delete(self, *args: Any, **kwargs: Any) -> NoReturn:
        raise AuditError("The audit log is append-only.")
