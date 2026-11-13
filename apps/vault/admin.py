"""The vault in the Django admin (dev and test only): READ-ONLY, metadata only.

No title (it may be sensitive and str() never shows it), no wrapped DEK, no
content: nothing can be added, changed or deleted, even by a superuser. Every
real operation goes through vault.services (rights, audit, encryption).
"""

from __future__ import annotations

from typing import Any

from django.contrib import admin
from django.http import HttpRequest

from .models import (
    AuditCheckpoint,
    AuditEvent,
    Document,
    DocumentAccessGrant,
    DocumentVersion,
    KeyEncryptionKey,
)


class ReadOnlyAdmin(admin.ModelAdmin):  # type: ignore[type-arg]
    actions = None  # no bulk delete

    def has_add_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        return False

    def has_change_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        return False

    def has_delete_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        return False

    def get_readonly_fields(self, request: HttpRequest, obj: Any = None) -> Any:
        return self.fields or self.list_display


@admin.register(Document)
class DocumentAdmin(ReadOnlyAdmin):
    list_display = ("id", "thesis", "confidentiality", "current_version", "owner", "updated_at")
    fields = (
        "id",
        "thesis",
        "confidentiality",
        "current_version",
        "owner",
        "created_at",
        "updated_at",
    )
    list_filter = ("confidentiality",)


@admin.register(DocumentVersion)
class DocumentVersionAdmin(ReadOnlyAdmin):
    # wrapped_dek is deliberately absent; so is any content.
    list_display = ("id", "document", "version", "kek_id", "size", "mime_type", "created_at")
    fields = (
        "id",
        "document",
        "version",
        "kek_id",
        "ciphertext_path",
        "size",
        "mime_type",
        "created_by",
        "created_at",
    )


@admin.register(DocumentAccessGrant)
class DocumentAccessGrantAdmin(ReadOnlyAdmin):
    list_display = ("id", "document", "user", "granted_by", "created_at", "revoked_at")
    fields = list_display


@admin.register(KeyEncryptionKey)
class KeyEncryptionKeyAdmin(ReadOnlyAdmin):
    list_display = ("kek_id", "provider", "domain", "version", "created_at", "retired_at")
    fields = list_display


@admin.register(AuditEvent)
class AuditEventAdmin(ReadOnlyAdmin):
    list_display = ("seq", "timestamp", "action", "actor", "thesis", "object_type", "object_id")
    fields = (
        "seq",
        "chain_format",
        "timestamp",
        "action",
        "actor",
        "thesis",
        "object_type",
        "object_id",
        "ip_hash",
        "details_json",
        "prev_hash",
        "hash",
    )
    list_filter = ("action",)


@admin.register(AuditCheckpoint)
class AuditCheckpointAdmin(ReadOnlyAdmin):
    list_display = ("seq", "timestamp", "hash")
    fields = ("seq", "timestamp", "hash", "signature")
