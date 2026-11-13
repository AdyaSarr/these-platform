"""Vault models: audit log, KEK registry, encrypted documents.

Stored in clear: metadata only (title, level, owner, sizes, mime type, paths).
Contents are AES-256-GCM blobs on disk (vault.storage); DEKs are stored only
wrapped under a KEK (vault.keys). No plaintext digest is stored (it would be a
confirmation oracle for whoever reads the database).
"""

from __future__ import annotations

import uuid
from typing import Any, ClassVar, NoReturn

from django.conf import settings
from django.contrib.contenttypes.fields import GenericForeignKey
from django.contrib.contenttypes.models import ContentType
from django.db import models
from django.db.models.functions import Cast, Concat, Length
from django.db.models.lookups import Exact
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.core.confidentiality import ConfidentialityLevel
from apps.core.models import TimeStampedModel

from .errors import AuditError
from .formats import LEVEL_TO_DOMAIN, MAX_VERSION, KeyDomain
from .validation import FILE_TYPES, MAX_UPLOAD_BYTES

ALLOWED_MIME_TYPES = sorted({t.mime_type for t in FILE_TYPES.values()})
WRAPPED_DEK_BYTES = 60  # nonce 12 + DEK 32 + tag 16
# Layout of vault.storage: <first 2 hex digits>/<32 hex digits>.blob
BLOB_PATH_REGEX = r"^([0-9a-f]{2})/\1[0-9a-f]{30}\.blob$"
_DOCUMENT_DOMAINS = "|".join(d.value for d in LEVEL_TO_DOMAIN.values())
DOCUMENT_KEK_ID_REGEX = rf"^[a-z][a-z0-9]{{0,15}}:({_DOCUMENT_DOMAINS}):[1-9][0-9]{{0,9}}$"


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


class Document(TimeStampedModel):
    """A document of a thesis: clear metadata, encrypted versions.

    ``confidentiality`` is part of every version's associated data: it changes
    only through vault.services.change_level(), which re-encrypts.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    thesis = models.ForeignKey(
        "accounts.Thesis", on_delete=models.PROTECT, related_name="documents", editable=False
    )
    title = models.CharField(_("title"), max_length=200)
    confidentiality = models.CharField(
        _("confidentiality"),
        max_length=20,
        choices=ConfidentialityLevel.choices,
        editable=False,
    )
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+", editable=False
    )
    current_version = models.PositiveBigIntegerField(default=1, editable=False)
    # Optional link to the business object the document belongs to (M3, M5...).
    related_content_type = models.ForeignKey(
        ContentType, on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    related_object_id = models.CharField(max_length=64, blank=True)
    related_object = GenericForeignKey("related_content_type", "related_object_id")

    class Meta:
        verbose_name = _("document")
        verbose_name_plural = _("documents")
        ordering: ClassVar[list[str]] = ["thesis", "title"]
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.CheckConstraint(
                condition=models.Q(confidentiality__in=ConfidentialityLevel.values),
                name="vault_document_known_level",
            ),
            models.CheckConstraint(
                condition=models.Q(current_version__gte=1, current_version__lte=MAX_VERSION),
                name="vault_document_current_version_range",
            ),
            models.CheckConstraint(
                condition=~models.Q(title=""), name="vault_document_title_not_empty"
            ),
        ]

    def __str__(self) -> str:
        return self.title


class DocumentVersion(TimeStampedModel):
    """One encrypted version: blob path, wrapped DEK and the KEK that wraps it."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    document = models.ForeignKey(
        Document, on_delete=models.PROTECT, related_name="versions", editable=False
    )
    version = models.PositiveBigIntegerField(editable=False)
    ciphertext_path = models.CharField(max_length=40, unique=True, editable=False)
    wrapped_dek = models.BinaryField(editable=False)
    kek_id = models.CharField(max_length=64, editable=False)
    size = models.PositiveBigIntegerField(_("size"), editable=False)  # of the plaintext
    mime_type = models.CharField(_("type"), max_length=100, editable=False)

    class Meta:
        verbose_name = _("document version")
        verbose_name_plural = _("document versions")
        ordering: ClassVar[list[str]] = ["document", "version"]
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.UniqueConstraint(
                fields=["document", "version"], name="vault_version_document_version"
            ),
            models.CheckConstraint(
                condition=models.Q(version__gte=1, version__lte=MAX_VERSION),
                name="vault_version_range",
            ),
            models.CheckConstraint(
                condition=models.Q(ciphertext_path__regex=BLOB_PATH_REGEX),
                name="vault_version_blob_path_layout",
            ),
            models.CheckConstraint(
                condition=Exact(Length("wrapped_dek"), WRAPPED_DEK_BYTES),
                name="vault_version_wrapped_dek_length",
            ),
            models.CheckConstraint(
                condition=models.Q(kek_id__regex=DOCUMENT_KEK_ID_REGEX),
                name="vault_version_kek_id_format",
            ),
            models.CheckConstraint(
                condition=models.Q(mime_type__in=ALLOWED_MIME_TYPES),
                name="vault_version_allowed_mime_type",
            ),
            models.CheckConstraint(
                condition=models.Q(size__gte=1, size__lte=MAX_UPLOAD_BYTES),
                name="vault_version_size_range",
            ),
        ]
        indexes: ClassVar[list[models.Index]] = [models.Index(fields=["kek_id"])]

    def __str__(self) -> str:
        return f"{self.document_id} v{self.version}"


class DocumentAccessGrant(TimeStampedModel):
    """Explicit access to one CONFIDENTIAL document (the "liste explicite").

    Valid only while the grantee has an active membership of the thesis
    (checked by accounts.permissions.can_access_document, not stored here).
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    document = models.ForeignKey(
        Document, on_delete=models.PROTECT, related_name="grants", editable=False
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+", editable=False
    )
    granted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+", editable=False
    )
    revoked_at = models.DateTimeField(null=True, blank=True, editable=False)

    class Meta:
        verbose_name = _("document access grant")
        verbose_name_plural = _("document access grants")
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.UniqueConstraint(
                fields=["document", "user"],
                condition=models.Q(revoked_at__isnull=True),
                name="vault_grant_one_active_per_user",
            ),
            models.CheckConstraint(
                condition=~models.Q(user=models.F("granted_by")),
                name="vault_grant_not_to_oneself",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.document_id} -> {self.user_id}"
