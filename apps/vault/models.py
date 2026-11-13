"""Vault models: audit log, KEK registry (M2 adds documents and the hash chain)."""

from __future__ import annotations

import uuid
from typing import Any, ClassVar, NoReturn

from django.conf import settings
from django.db import models
from django.db.models.functions import Cast, Concat
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.core.models import TimeStampedModel

from .errors import AuditError
from .formats import MAX_VERSION, KeyDomain


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


class KeyEncryptionKey(TimeStampedModel):
    """Metadata of one KEK version. Holds NO key material.

    The key itself lives in the provider (derived from MASTER_KEY in
    development, inside OpenBao in production). ``kek_id`` is computed by
    PostgreSQL from (provider, domain, version), so it cannot be written apart
    from them. Rows are never deleted; a version is retired by vault.keys.retire().
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    provider = models.CharField(max_length=16, editable=False)
    domain = models.CharField(
        max_length=32, choices=[(d.value, d.value) for d in KeyDomain], editable=False
    )
    version = models.PositiveBigIntegerField(editable=False)
    # Every operand cast to text: one type for Concat, and casts that PostgreSQL
    # accepts as IMMUTABLE in a generated column.
    kek_id = models.GeneratedField(
        expression=Concat(
            Cast("provider", models.TextField()),
            models.Value(":", output_field=models.TextField()),
            Cast("domain", models.TextField()),
            models.Value(":", output_field=models.TextField()),
            Cast("version", models.TextField()),
            output_field=models.TextField(),
        ),
        output_field=models.CharField(max_length=64),
        db_persist=True,
    )
    retired_at = models.DateTimeField(null=True, blank=True, editable=False)

    class Meta:
        verbose_name = _("key-encryption key")
        verbose_name_plural = _("key-encryption keys")
        ordering: ClassVar[list[str]] = ["domain", "version"]
        constraints: ClassVar[list[models.BaseConstraint]] = [
            # Versions are numbered per domain across providers (env v1..3, then bao v4).
            models.UniqueConstraint(fields=["domain", "version"], name="vault_kek_domain_version"),
            models.UniqueConstraint(fields=["kek_id"], name="vault_kek_kek_id"),
            models.CheckConstraint(
                condition=models.Q(version__gte=1, version__lte=MAX_VERSION),
                name="vault_kek_version_range",
            ),
            models.CheckConstraint(
                condition=models.Q(domain__in=[d.value for d in KeyDomain]),
                name="vault_kek_known_domain",
            ),
            models.CheckConstraint(
                condition=models.Q(provider__regex=r"^[a-z][a-z0-9]{0,15}$"),
                name="vault_kek_provider_format",
            ),
        ]

    def __str__(self) -> str:
        return str(self.kek_id)
