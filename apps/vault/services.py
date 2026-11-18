"""Vault services: the only door to encrypted documents, sealed texts, MFA secrets
and the audit log.

Every function here follows the same rules (docs/modules/M02-coffre.md):
- rights are checked HERE (accounts.permissions), even if a view did it too; a
  refusal is audited in its own transaction, then PermissionDenied is raised;
- lock order: KEK lock (vault.keys), then row locks, then audit() LAST;
- blob before row: a failed transaction leaves an orphan blob whose DEK was
  never stored (unreadable), never a row without its blob;
- functions holding a DEK or a plaintext carry @sensitive_variables().
Re-authentication is a view concern (it needs the request).

The audit log itself lives in vault.audit; ``audit``, ``AuditAction``,
``canonical_ip``, ``client_ip`` and ``FORBIDDEN_KEY_FRAGMENTS`` are re-exported here for the
modules that import them from vault.services.

Call each service in its OWN transaction (the default without ATOMIC_REQUESTS):
inside one enclosing transaction, the audit() of a first call would precede the
KEK lock of the next one, which vault.keys refuses (LockOrderError).
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.utils import timezone
from django.utils.text import slugify
from django.utils.translation import gettext as _
from django.views.decorators.debug import sensitive_variables

from apps.accounts.models import Thesis, User
from apps.accounts.permissions import Action, can, can_access_document, can_access_level, roles_of
from apps.activity import events as activity
from apps.core.confidentiality import ConfidentialityLevel

from . import crypto, formats, keys, locks, storage
from .audit import (
    FORBIDDEN_KEY_FRAGMENTS,
    AuditAction,
    audit,
    audit_source,
    canonical_ip,
    client_ip,
)
from .errors import (
    INTEGRITY_MESSAGE,
    AuditError,
    BlobNotFoundError,
    LockOrderError,
    ReauthenticationRequiredError,
    StorageError,
    UnknownKekError,
    VaultIntegrityError,
    VaultKeyError,
)
from .formats import LEVEL_TO_DOMAIN, KeyDomain
from .models import Document, DocumentAccessGrant, DocumentVersion, SealedText
from .validation import FILE_TYPES, validate_upload

if TYPE_CHECKING:
    from collections.abc import Mapping

    from .validation import Upload

__all__ = [
    "FORBIDDEN_KEY_FRAGMENTS",
    "RESTRICTED",
    "AuditAction",
    "AuditError",
    "Opened",
    "TextSealer",
    "add_version",
    "audit",
    "audit_source",
    "canonical_ip",
    "change_level",
    "client_ip",
    "decrypt_secret",
    "document_for",
    "download_name",
    "encrypt_secret",
    "grant_access",
    "open_text",
    "open_texts",
    "open_version",
    "readable_documents",
    "revoke_access",
    "rewrap_batch",
    "sealing",
    "store",
]


def vault_problems() -> list[str]:
    """What `manage.py vault_init` has not done yet (empty when the vault is ready)."""
    problems = []
    if not storage.root().is_dir():
        problems.append("the storage directory (VAULT_STORAGE_DIR) does not exist")
    missing = keys.missing_domains()
    if missing:
        problems.append("no KEK version for " + ", ".join(d.value for d in missing))
    return problems


# =========================================================================== documents

DOMAIN_TO_LEVEL: Mapping[KeyDomain, ConfidentialityLevel] = MappingProxyType(
    {domain: level for level, domain in LEVEL_TO_DOMAIN.items()}
)
_MIME_EXTENSION = {t.mime_type: t.extension for t in FILE_TYPES.values()}


@dataclass(frozen=True)
class Opened:
    """A decrypted version, for the download view (which also hides its locals)."""

    data: bytes = field(repr=False)
    mime_type: str
    download_name: str


def _level(document: Document) -> ConfidentialityLevel:
    return ConfidentialityLevel(document.confidentiality)


def _content_ad(document: Document, version: int, level: ConfidentialityLevel) -> bytes:
    return formats.content_ad(
        thesis_id=document.thesis_id, document_id=document.pk, version=version, level=level
    )


def _wrap_ad(
    document: Document, version: int, level: ConfidentialityLevel
) -> Callable[[str], bytes]:
    def ad_for(kek_id: str) -> bytes:
        return formats.dek_wrap_ad(
            thesis_id=document.thesis_id,
            document_id=document.pk,
            version=version,
            level=level,
            kek_id=kek_id,
        )

    return ad_for


def _audit_now(action: AuditAction, **kwargs: Any) -> None:
    """Audit in its own transaction: kept even though an exception follows."""
    with transaction.atomic():
        audit(action, **kwargs)


def _deny(user: Any, operation: str, **kwargs: Any) -> PermissionDenied:
    _audit_now(AuditAction.DOCUMENT_ACCESS_DENIED, actor=user, operation=operation, **kwargs)
    return PermissionDenied()


@sensitive_variables()
def _seal_blob(
    document: Document, version: int, level: ConfidentialityLevel, data: bytes
) -> tuple[str, bytes]:
    """Encrypt under a fresh DEK and write the blob; returns (blob path, DEK)."""
    dek = crypto.new_dek()
    sealed = crypto.aead_encrypt(dek, data, _content_ad(document, version, level))
    return storage.write(formats.pack_blob(sealed)), dek


@sensitive_variables()
def _decrypt(version: DocumentVersion, document: Document, level: ConfidentialityLevel) -> bytes:
    """Read, unwrap and decrypt one version, the level being that of ``document``."""
    blob = storage.read(version.ciphertext_path)
    ad_for = _wrap_ad(document, version.version, level)
    dek = keys.unwrap(version.kek_id, bytes(version.wrapped_dek), ad_for(version.kek_id))
    content_ad = _content_ad(document, version.version, level)
    return crypto.aead_decrypt(dek, formats.unpack_blob(blob), content_ad)


def _audit_failure(exc: Exception, *, user: Any, document: Document, version: int) -> None:
    """Classify a decryption failure: an integrity alert must mean an integrity problem."""
    if isinstance(exc, VaultIntegrityError | BlobNotFoundError):
        action = AuditAction.DOCUMENT_INTEGRITY_FAILED
    elif isinstance(exc, UnknownKekError):
        action = AuditAction.DOCUMENT_KEK_UNAVAILABLE
    else:
        action = AuditAction.DOCUMENT_STORAGE_ERROR
    _audit_now(action, actor=user, obj=document, version=version, error=type(exc).__name__)


def download_name(title: str, mime_type: str) -> str:
    """File name for the download: the cleaned title and OUR extension, never the
    name given at upload time."""
    stem = slugify(title)[:80].strip("-_") or "document"
    return f"{stem}.{_MIME_EXTENSION[mime_type]}"


def _clean_title(title: str) -> str:
    title = (title or "").strip()
    if not title:
        raise ValidationError(_("A title is required."), code="title")
    return title[:200]


@sensitive_variables()
def store(
    *,
    thesis: Thesis,
    upload: Upload,
    title: str,
    level: ConfidentialityLevel,
    user: User,
    announce: bool = True,
) -> Document:
    """Store a new document (version 1).

    ``announce=False``: no « Nouveau document » in the activity feed, because the
    caller's own event covers it (a piece of the administrative file, M13a review).
    The audit journal is the same.

    Outside any transaction and lock: validation, encryption, blob written (the
    content AD does not contain the kek_id). Then one transaction: wrap the DEK
    (shared KEK lock), create the rows, audit last.
    """
    if not isinstance(level, ConfidentialityLevel):
        raise ValueError("level must be a ConfidentialityLevel member")
    if not (can(user, Action.DOCUMENT_WRITE, thesis) and can_access_level(user, thesis, level)):
        raise _deny(user, "store", thesis=thesis if thesis.pk else None, level=level.value)
    title = _clean_title(title)
    checked = validate_upload(upload)
    document = Document(
        thesis=thesis,
        title=title,
        confidentiality=level,
        owner=user,
        created_by=user,
        current_version=1,
    )
    path, dek = _seal_blob(document, 1, level, checked.data)
    with transaction.atomic():
        kek_id, wrapped = keys.wrap(LEVEL_TO_DOMAIN[level], dek, _wrap_ad(document, 1, level))
        document.save(force_insert=True)
        DocumentVersion.objects.create(
            document=document,
            version=1,
            ciphertext_path=path,
            wrapped_dek=wrapped,
            kek_id=kek_id,
            size=checked.size,
            mime_type=checked.mime_type,
            created_by=user,
        )
        if announce:
            activity.record(
                kind="DOCUMENT_ADDED", obj=document, object_type="DOCUMENT",
                thesis=document.thesis, actor=user,
            )  # fmt: skip
        audit(
            AuditAction.DOCUMENT_CREATED,
            actor=user,
            obj=document,
            version=1,
            level=level.value,
            size=checked.size,
            mime=checked.mime_type,
        )
    return document


@sensitive_variables()
def add_version(
    *, document: Document, upload: Upload, user: User, announce: bool = True
) -> DocumentVersion:
    """Add the next version. The version number is in the AD and is known only once
    the document row is locked: encryption and blob happen inside the transaction.
    ``announce=False``: no activity line (see store()).
    """
    checked = None
    for _attempt in range(2):  # retried once if the level changed meanwhile
        fresh = Document.objects.get(pk=document.pk)
        level = _level(fresh)
        if not (
            can(user, Action.DOCUMENT_WRITE, fresh) and can_access_level(user, fresh.thesis, level)
        ):
            raise _deny(user, "add_version", obj=fresh)
        checked = checked or validate_upload(upload)
        domain = LEVEL_TO_DOMAIN[level]
        with transaction.atomic():
            keys.current_kek(domain)  # shared KEK lock first
            locked = Document.objects.select_for_update().get(pk=document.pk)
            if _level(locked) != level:
                continue  # change_level() committed in between: start again
            number = locked.current_version + 1
            path, dek = _seal_blob(locked, number, level, checked.data)
            kek_id, wrapped = keys.wrap(domain, dek, _wrap_ad(locked, number, level))
            version = DocumentVersion.objects.create(
                document=locked,
                version=number,
                ciphertext_path=path,
                wrapped_dek=wrapped,
                kek_id=kek_id,
                size=checked.size,
                mime_type=checked.mime_type,
                created_by=user,
            )
            locked.current_version = number
            locked.save(update_fields=["current_version", "updated_at"])
            if announce:
                activity.record(
                    kind="DOCUMENT_VERSION_ADDED", obj=locked, object_type="DOCUMENT",
                    thesis=locked.thesis, actor=user,
                )  # fmt: skip
            audit(AuditAction.DOCUMENT_VERSION_ADDED, actor=user, obj=locked, version=number)
            return version
    raise RuntimeError("the document's level changed twice during add_version(); retry")


def readable_documents(user: Any, thesis: Thesis) -> list[Document]:
    """The documents of ``thesis`` that ``user`` may read (can_access_document: role,
    level, explicit grant, module restrictions), by title. Nothing is decrypted."""
    return [
        d
        for d in Document.objects.filter(thesis=thesis).order_by("title")
        if can_access_document(user, d)
    ]


def latest_documents(user: Any, thesis: Thesis, *, limit: int = 3) -> list[Document]:
    """The ``limit`` documents last updated among those ``user`` may read (the
    list's own rule, readable_documents). Read only; nothing is decrypted."""
    if not can(user, Action.DOCUMENT_READ, thesis):
        raise _deny(user, "latest", thesis=thesis)
    readable = readable_documents(user, thesis)
    return sorted(readable, key=lambda d: (d.updated_at, str(d.pk)), reverse=True)[:limit]


def dashboard_block(request: Any, thesis: Thesis) -> dict[str, Any]:
    """Data of the thesis dashboard card (accounts.views.DASHBOARD_BLOCKS): the
    number of documents the user may read, the readable ones (to number the
    repeated titles like the list) and the latest three. Nothing decrypted."""
    readable = readable_documents(request.user, thesis)
    return {
        "count": len(readable),
        "readable": readable,
        "latest": latest_documents(request.user, thesis),
    }


def document_for(user: User, thesis: Thesis, document_id: Any, *, operation: str) -> Document:
    """The document ``document_id`` of ``thesis`` if ``user`` may read it.

    Unknown, of another thesis than the URL's, or not readable: the same
    PermissionDenied, audited (IDOR: the id in the URL never crosses theses).
    """
    try:
        document = Document.objects.filter(pk=document_id, thesis=thesis).first()
    except (ValueError, ValidationError):
        document = None
    if document is None or not can_access_document(user, document):
        details: dict[str, Any] = {"obj": document} if document is not None else {}
        raise _deny(user, operation, thesis=thesis, **details)
    return document


@sensitive_variables()
def open_version(
    version_id: Any, user: User, *, thesis: Thesis | None = None, recent_mfa: bool = False
) -> Opened:
    """Decrypt one version for ``user``; every read is audited.

    ``thesis``: the thesis of the URL; a version of another thesis is refused
    (IDOR). ``recent_mfa``: whether the caller verified a second factor less
    than 10 minutes old; a CONFIDENTIAL version is never opened without it
    (ReauthenticationRequiredError, raised before anything is read).

    The version and its document are read in ONE query: the level used in the
    AD and the wrapped DEK come from the same state of the database (a
    change_level() committed meanwhile cannot pair them wrongly).
    """
    try:
        version = DocumentVersion.objects.select_related("document").filter(pk=version_id).first()
    except (ValueError, ValidationError):  # malformed id: same answer as forbidden
        version = None
    if version is not None and thesis is not None and version.document.thesis_id != thesis.pk:
        raise _deny(user, "read", thesis=thesis)  # another thesis than the URL's
    if version is None or not can_access_document(user, version.document):
        details: dict[str, Any] = {"obj": version.document} if version is not None else {}
        if thesis is not None and version is None:
            details["thesis"] = thesis
        raise _deny(user, "read", **details)
    document = version.document
    level = _level(document)
    if level is ConfidentialityLevel.CONFIDENTIAL and not recent_mfa:
        raise ReauthenticationRequiredError()
    try:
        data = _decrypt(version, document, level)
    except (VaultIntegrityError, UnknownKekError, StorageError) as exc:
        _audit_failure(exc, user=user, document=document, version=version.version)
        raise
    with transaction.atomic():
        audit(AuditAction.DOCUMENT_READ, actor=user, obj=document, version=version.version)
    name = download_name(document.title, version.mime_type)
    return Opened(data=data, mime_type=version.mime_type, download_name=name)


@sensitive_variables()
def change_level(*, document: Document, level: ConfidentialityLevel, user: User) -> None:
    """Move a document to another level: every version re-encrypted under a NEW DEK
    and the new level's AD, in new blobs (the old ones become orphans whose DEK no
    longer exists in the database); grants revoked when leaving CONFIDENTIAL.
    """
    if not isinstance(level, ConfidentialityLevel):
        raise ValueError("level must be a ConfidentialityLevel member")
    fresh = Document.objects.get(pk=document.pk)
    old_level = _level(fresh)
    if not (
        can(user, Action.DOCUMENT_WRITE, fresh)
        and can_access_level(user, fresh.thesis, old_level)
        and can_access_level(user, fresh.thesis, level)
    ):
        raise _deny(user, "change_level", obj=fresh, level=level.value)
    if level == old_level:
        raise ValueError("the document already has this level")
    try:
        _change_level(fresh, old_level, level, user)
    except (VaultIntegrityError, UnknownKekError, StorageError) as exc:
        _audit_failure(exc, user=user, document=fresh, version=fresh.current_version)
        raise


@sensitive_variables()
def _change_level(
    document: Document, old_level: ConfidentialityLevel, level: ConfidentialityLevel, user: User
) -> None:
    domain = LEVEL_TO_DOMAIN[level]
    with transaction.atomic():
        keys.current_kek(domain)  # shared KEK lock first
        locked = Document.objects.select_for_update().get(pk=document.pk)
        if _level(locked) != old_level:
            raise ValueError("the document's level changed meanwhile; retry")
        versions = list(locked.versions.select_for_update().order_by("version"))
        for version in versions:
            data = _decrypt(version, locked, old_level)
            path, dek = _seal_blob(locked, version.version, level, data)
            kek_id, wrapped = keys.wrap(domain, dek, _wrap_ad(locked, version.version, level))
            version.ciphertext_path, version.wrapped_dek, version.kek_id = path, wrapped, kek_id
            version.save(update_fields=["ciphertext_path", "wrapped_dek", "kek_id", "updated_at"])
        locked.confidentiality = level
        locked.save(update_fields=["confidentiality", "updated_at"])
        revoked = 0
        if level is not ConfidentialityLevel.CONFIDENTIAL:
            revoked = locked.grants.filter(revoked_at__isnull=True).update(
                revoked_at=timezone.now()
            )
        audit(
            AuditAction.DOCUMENT_LEVEL_CHANGED,
            actor=user,
            obj=locked,
            old_level=old_level.value,
            new_level=level.value,
            versions=len(versions),
            grants_revoked=revoked,
        )


# =========================================================================== explicit grants


def grant_access(*, document: Document, grantee: User, user: User) -> DocumentAccessGrant:
    """Explicit access to one CONFIDENTIAL document ("+ liste explicite")."""
    if not can(user, Action.DOCUMENT_GRANT, document):
        raise _deny(user, "grant", obj=document)
    with transaction.atomic():
        locked = Document.objects.select_for_update().get(pk=document.pk)
        if _level(locked) is not ConfidentialityLevel.CONFIDENTIAL:
            raise ValidationError(
                _("Only confidential documents take explicit access."), code="not_confidential"
            )
        if grantee.pk == user.pk:
            raise ValidationError(_("You already have access."), code="self")
        if not roles_of(grantee, locked.thesis):
            raise ValidationError(
                _("Access can only be given to an active member of the thesis."), code="member"
            )
        existing = locked.grants.filter(user=grantee, revoked_at__isnull=True).first()
        if existing is not None:
            return existing
        grant = DocumentAccessGrant.objects.create(
            document=locked, user=grantee, granted_by=user, created_by=user
        )
        activity.record(
            kind="DOCUMENT_GRANTED", obj=grant, object_type="DOCUMENT_GRANT",
            thesis=locked.thesis, actor=user,
        )  # fmt: skip
        audit(AuditAction.DOCUMENT_GRANT_ADDED, actor=user, obj=locked, grantee=str(grantee.pk))
    return grant


def revoke_access(*, grant: DocumentAccessGrant, user: User) -> None:
    if not can(user, Action.DOCUMENT_GRANT, grant.document):
        raise _deny(user, "revoke", obj=grant.document)
    with transaction.atomic():
        locked = DocumentAccessGrant.objects.select_for_update().get(pk=grant.pk)
        if locked.revoked_at is not None:
            return
        locked.revoked_at = timezone.now()
        locked.save(update_fields=["revoked_at", "updated_at"])
        audit(
            AuditAction.DOCUMENT_GRANT_REVOKED,
            actor=user,
            obj=grant.document,
            grantee=str(locked.user_id),
        )


# =========================================================================== sealed texts
#
# Short texts of business objects (titles, notes, comments), one SealedText row
# per (slot, object): the slot names the model and the field. Each value is
# padded (ISO/IEC 7816-4, 64-byte blocks), sealed under a DEK of its own, the
# DEK wrapped under the KEK of the level. AD = (thesis, object, slot, level).
#
# Writing goes through sealing(), a context manager that checks the rights,
# opens THE transaction and takes the KEK lock; the business service then locks
# its rows, seals, and calls audit() last, inside the block:
#
#     with services.sealing(user=u, thesis=t, level=lvl, action=Action.OBJECTIVE_WRITE) as s:
#         objective = Objective.objects.select_for_update().get(pk=...)
#         s.seal(objective, "title", title)
#         audit(AuditAction.OBJECTIVE_REVISED, ...)


class _Restricted:
    """What open_texts() gives for a text whose level the reader may not see."""

    _instance: _Restricted | None = None

    def __new__(cls) -> _Restricted:
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __repr__(self) -> str:
        return "RESTRICTED"

    def __bool__(self) -> bool:
        return False


RESTRICTED = _Restricted()

TextKey = tuple[uuid.UUID, str]  # (object pk, field name)


def _slot_of(obj: Any, name: str) -> tuple[int, uuid.UUID]:
    """(slot code, object id) of ``obj.<name>``; the slot is named by the model's label."""
    meta = getattr(obj, "_meta", None)
    label = getattr(meta, "label", None)
    code = formats.slot_code(f"{label}.{name}")
    if not isinstance(obj.pk, uuid.UUID):
        raise TypeError("a sealed text belongs to an object with a UUID primary key")
    return code, obj.pk


def _text_ad(row: SealedText, level: ConfidentialityLevel) -> bytes:
    return formats.text_ad(
        thesis_id=row.thesis_id, object_id=row.object_id, slot=row.slot, level=level
    )


def _text_wrap_ad(row: SealedText, level: ConfidentialityLevel) -> Callable[[str], bytes]:
    def ad_for(kek_id: str) -> bytes:
        return formats.text_dek_wrap_ad(
            thesis_id=row.thesis_id,
            object_id=row.object_id,
            slot=row.slot,
            level=level,
            kek_id=kek_id,
        )

    return ad_for


def _text_bytes(text: str) -> bytes:
    """UTF-8 of ``text``: no NUL (PostgreSQL text refuses it), no lone surrogate."""
    if not isinstance(text, str):
        raise TypeError("a sealed text must be a str")
    if "\x00" in text:
        raise ValueError("NUL character in a text")
    data = text.encode("utf-8")  # UnicodeEncodeError (a ValueError) on a lone surrogate
    if len(data) > formats.MAX_TEXT_BYTES:
        raise ValueError(f"text too long: at most {formats.MAX_TEXT_BYTES} bytes in UTF-8")
    return data


@sensitive_variables()
def _seal_text_row(row: SealedText, level: ConfidentialityLevel, data: bytes) -> None:
    """Seal ``data`` into ``row`` (not saved) under a fresh DEK; KEK lock already held."""
    dek = crypto.new_dek()
    row.level = level.value
    sealed = crypto.aead_encrypt(dek, formats.pad_text(data), _text_ad(row, level))
    row.ciphertext = formats.pack_blob(sealed)
    row.kek_id, row.wrapped_dek = keys.wrap(LEVEL_TO_DOMAIN[level], dek, _text_wrap_ad(row, level))


@sensitive_variables()
def _open_text_row(row: SealedText, level: ConfidentialityLevel) -> bytes:
    """Unwrap, decrypt and unpad one row, the level being the row's (it is in the AD)."""
    ad_for = _text_wrap_ad(row, level)
    dek = keys.unwrap(row.kek_id, bytes(row.wrapped_dek), ad_for(row.kek_id))
    sealed = formats.unpack_blob(bytes(row.ciphertext))
    padded = crypto.aead_decrypt(dek, sealed, _text_ad(row, level))
    return formats.unpad_text(padded)


def _decode(data: bytes) -> str:
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:  # authentic but not what _text_bytes wrote
        raise VaultIntegrityError(INTEGRITY_MESSAGE) from None


def _audit_text_failure(exc: Exception, *, user: Any, row: SealedText) -> None:
    if isinstance(exc, VaultIntegrityError):
        action = AuditAction.SEALED_TEXT_INTEGRITY_FAILED
    else:
        action = AuditAction.SEALED_TEXT_KEK_UNAVAILABLE
    _audit_now(
        action,
        actor=user,
        thesis=Thesis.objects.get(pk=row.thesis_id),
        slot=formats.SLOT_NAMES[row.slot],
        object_ids=[str(row.object_id)],
        error=type(exc).__name__,
    )


def _deny_text(user: Any, operation: str, **kwargs: Any) -> PermissionDenied:
    _audit_now(AuditAction.SEALED_TEXT_ACCESS_DENIED, actor=user, operation=operation, **kwargs)
    return PermissionDenied()


class TextSealer:
    """Seals texts of one thesis at one level; valid only inside its sealing() block."""

    def __init__(self, *, user: User, thesis: Thesis, level: ConfidentialityLevel) -> None:
        self._user = user
        self._thesis = thesis
        self._level = level
        self._closed = False
        self._failed_row: SealedText | None = None

    @property
    def level(self) -> ConfidentialityLevel:
        return self._level

    def _usable(self, obj: Any, name: str) -> tuple[int, uuid.UUID]:
        if self._closed:
            raise RuntimeError("this sealer is closed: seal inside its sealing() block")
        if locks.held(locks.AUDIT_NAMESPACE, locks.AUDIT_CHAIN_KEY):
            # keys.wrap would accept it (the KEK lock is already held), but the
            # convention is audit() LAST: nothing is sealed after it.
            raise LockOrderError("seal requested after audit() in the same transaction")
        code, object_id = _slot_of(obj, name)
        if getattr(obj, "thesis_id", None) != self._thesis.pk:
            raise ValueError("the object belongs to another thesis than the sealer's")
        return code, object_id

    def _locked_row(self, code: int, object_id: uuid.UUID) -> SealedText | None:
        row = SealedText.objects.select_for_update().filter(slot=code, object_id=object_id).first()
        if row is not None and row.thesis_id != self._thesis.pk:
            raise VaultIntegrityError(INTEGRITY_MESSAGE)
        return row

    @sensitive_variables()
    def seal(self, obj: Any, name: str, text: str) -> None:
        """Store ``text`` as ``obj.<name>``, replacing the previous value (fresh DEK)."""
        code, object_id = self._usable(obj, name)
        data = _text_bytes(text)
        row = self._locked_row(code, object_id) or SealedText(
            thesis=self._thesis, slot=code, object_id=object_id, created_by=self._user
        )
        _seal_text_row(row, self._level, data)
        row.save()

    def erase(self, obj: Any, name: str) -> bool:
        """Delete ``obj.<name>``; returns whether there was one."""
        code, object_id = self._usable(obj, name)
        row = self._locked_row(code, object_id)
        if row is None:
            return False
        row.delete()
        return True

    @sensitive_variables()
    def reseal(self, obj: Any, name: str) -> bool:
        """Move ``obj.<name>`` to the sealer's level (new DEK); returns whether it moved.

        The caller checked that the user may read the old level: refused otherwise.
        """
        code, object_id = self._usable(obj, name)
        row = self._locked_row(code, object_id)
        if row is None or row.level == self._level:
            return False
        old = ConfidentialityLevel(row.level)
        if not can_access_level(self._user, self._thesis, old):
            raise PermissionDenied()
        try:
            data = _open_text_row(row, old)
        except (VaultIntegrityError, UnknownKekError):
            self._failed_row = row  # audited by sealing(), once this transaction is rolled back
            raise
        _seal_text_row(row, self._level, data)
        row.save()
        return True


@contextmanager
def sealing(
    *, user: User, thesis: Thesis, level: ConfidentialityLevel, action: Action
) -> Iterator[TextSealer]:
    """Open the transaction in which ``user`` seals texts of ``thesis`` at ``level``.

    Rights first, outside any transaction (a refusal is audited and kept):
    ``action`` on the thesis AND read access to ``level``. Then
    transaction.atomic() and the KEK lock (shared) of the level, before any row
    lock of the caller. The caller's audit() is the last call of the block.
    """
    if not isinstance(level, ConfidentialityLevel):
        raise ValueError("level must be a ConfidentialityLevel member")
    if not isinstance(action, Action):
        raise ValueError("action must be an Action member")
    if not (can(user, action, thesis) and can_access_level(user, thesis, level)):
        raise _deny_text(user, "seal", thesis=thesis if thesis.pk else None, level=level.value)
    sealer = TextSealer(user=user, thesis=thesis, level=level)
    try:
        with transaction.atomic():
            keys.current_kek(LEVEL_TO_DOMAIN[level])  # shared KEK lock first
            yield sealer
    except (VaultIntegrityError, UnknownKekError) as exc:
        if sealer._failed_row is not None:  # reseal() could not open the old value
            _audit_text_failure(exc, user=user, row=sealer._failed_row)
        raise
    finally:
        sealer._closed = True


@sensitive_variables()
def open_texts(
    user: User, thesis: Thesis, items: Iterable[tuple[Any, str]], *, action: Action
) -> dict[TextKey, str | _Restricted]:
    """Decrypt the texts ``(obj, field)`` of ``thesis`` for ``user``, in one query.

    Result keyed by (obj.pk, field): the text, RESTRICTED when the user may not
    read its level, absent when there is no text. Refused (audited) without
    ``action`` on the thesis, or for an object of another thesis (IDOR).
    Reads of CONFIDENTIAL texts are audited, one event per call, after
    decryption; an integrity or KEK failure is audited and raised.
    """
    if not isinstance(action, Action):
        raise ValueError("action must be an Action member")
    if not can(user, action, thesis):
        raise _deny_text(user, "read", thesis=thesis if thesis.pk else None)
    wanted: dict[tuple[int, uuid.UUID], TextKey] = {}
    for obj, name in items:
        code, object_id = _slot_of(obj, name)
        if getattr(obj, "thesis_id", None) != thesis.pk:
            raise _deny_text(user, "read", thesis=thesis, reason="other_thesis")
        wanted[(code, object_id)] = (object_id, name)
    if not wanted:
        return {}
    rows = SealedText.objects.filter(
        thesis=thesis,
        slot__in={code for code, _ in wanted},
        object_id__in={object_id for _, object_id in wanted},
    ).order_by("pk")
    readable: dict[ConfidentialityLevel, bool] = {}
    result: dict[TextKey, str | _Restricted] = {}
    confidential: list[SealedText] = []
    for row in rows:
        key = wanted.get((row.slot, row.object_id))
        if key is None:  # another (slot, object) pair of the same sets
            continue
        level = ConfidentialityLevel(row.level)
        if level not in readable:
            readable[level] = can_access_level(user, thesis, level)
        if not readable[level]:
            result[key] = RESTRICTED
            continue
        try:
            result[key] = _decode(_open_text_row(row, level))
        except (VaultIntegrityError, UnknownKekError) as exc:
            _audit_text_failure(exc, user=user, row=row)
            raise
        if level is ConfidentialityLevel.CONFIDENTIAL:
            confidential.append(row)
    if confidential:
        _audit_now(
            AuditAction.SEALED_TEXT_READ,
            actor=user,
            thesis=thesis,
            level=ConfidentialityLevel.CONFIDENTIAL.value,
            count=len(confidential),
            slots=sorted({formats.SLOT_NAMES[row.slot] for row in confidential}),
            object_ids=sorted({str(row.object_id) for row in confidential}),
        )
    return result


@sensitive_variables()
def open_text(user: User, obj: Any, name: str, *, action: Action) -> str | _Restricted:
    """One text of ``obj`` (its thesis is the object's): "" when there is none."""
    thesis = Thesis.objects.get(pk=obj.thesis_id)
    return open_texts(user, thesis, [(obj, name)], action=action).get((obj.pk, name), "")


def sealed_texts_using(kek_id: str) -> int:
    """Reference counter of the four level domains (registered in VaultConfig.ready)."""
    return SealedText.objects.filter(kek_id=kek_id).count()


# =========================================================================== MFA secrets


def _mfa_ad(kek_id: str) -> bytes:
    return formats.mfa_secret_ad(kek_id=kek_id)


@sensitive_variables()
def encrypt_secret(text: str) -> str:
    """Seal an MFA secret (TOTP key, recovery-code seed) under the MFA_SECRETS KEK.

    allauth calls this outside a transaction and stores the result afterwards:
    the retirement grace period (VAULT_RETIRE_GRACE) covers that gap.
    """
    with transaction.atomic():
        kek_id, sealed = keys.wrap(KeyDomain.MFA_SECRETS, text.encode("utf-8"), _mfa_ad)
    return formats.pack_secret(kek_id, sealed)


@sensitive_variables()
def decrypt_secret(text: str) -> str:
    """Inverse of encrypt_secret; a clear or malformed value is refused."""
    kek_id, sealed = formats.unpack_secret(text)
    return keys.unwrap(kek_id, sealed, _mfa_ad(kek_id)).decode("utf-8")


MFA_SECRET_FIELDS = ("secret", "seed")  # allauth: TOTP key, recovery-code seed


def mfa_secrets_using(kek_id: str) -> int:
    """Reference counter of the MFA_SECRETS domain (registered in VaultConfig.ready).

    The kek_id is inside the base64 text: every value is decoded (a few dozen
    rows). A value that cannot be decoded is counted, so it blocks retirement.
    """
    from allauth.mfa.models import Authenticator

    count = 0
    for data in Authenticator.objects.values_list("data", flat=True):
        for name in MFA_SECRET_FIELDS:
            value = (data or {}).get(name)
            if not isinstance(value, str):
                continue
            try:
                used, _sealed = formats.unpack_secret(value)
            except VaultIntegrityError:
                count += 1  # unknown: deny retirement by default
                continue
            count += used == kek_id
    return count


# =========================================================================== rotation


@sensitive_variables()
def rewrap_batch(domain: KeyDomain, *, limit: int = 100) -> int:
    """Move up to ``limit`` rows of ``domain`` to its current KEK; returns how many.

    One short transaction: shared KEK lock, row locks, re-wrap, audit last.
    Documents and sealed texts keep their DEK and ciphertext (only the wrapped
    DEK changes); MFA secrets are re-sealed (they have no DEK).
    """
    with transaction.atomic():
        current = keys.current_kek(domain)
        if domain is KeyDomain.MFA_SECRETS:
            count = _rewrap_mfa(current, limit)
        else:
            count = _rewrap_documents(domain, current, limit)
            if count < limit:
                count += _rewrap_texts(domain, current, limit - count)
        if count:
            audit(AuditAction.KEK_REWRAPPED, domain=domain.value, kek_id=current, rows=count)
    return count


@sensitive_variables()
def _rewrap_documents(domain: KeyDomain, current: str, limit: int) -> int:
    level = DOMAIN_TO_LEVEL[domain]
    rows = list(
        DocumentVersion.objects.select_for_update(of=("self",))
        .select_related("document")
        .filter(document__confidentiality=level.value)
        .exclude(kek_id=current)
        .order_by("pk")[:limit]
    )
    count = 0
    for row in rows:
        # After waiting behind a change_level(), PostgreSQL re-reads the locked
        # version row (new kek_id, new DEK) but keeps the OLD read of the joined
        # document: trust the row's own kek_id, then the level.
        if keys.parse_kek_id(row.kek_id).domain is not domain:
            continue
        if _level(row.document) != level:  # moved by a concurrent change_level()
            continue
        ad_for = _wrap_ad(row.document, row.version, level)
        dek = keys.unwrap(row.kek_id, bytes(row.wrapped_dek), ad_for(row.kek_id))
        kek_id, wrapped = keys.wrap(domain, dek, ad_for)
        row.kek_id, row.wrapped_dek = kek_id, wrapped
        row.save(update_fields=["kek_id", "wrapped_dek", "updated_at"])
        count += 1
    return count


@sensitive_variables()
def _rewrap_texts(domain: KeyDomain, current: str, limit: int) -> int:
    level = DOMAIN_TO_LEVEL[domain]
    rows = list(
        SealedText.objects.select_for_update()
        .filter(level=level.value)
        .exclude(kek_id=current)
        .order_by("pk")[:limit]
    )
    count = 0
    for row in rows:
        # Re-read after waiting behind a reseal(): trust the row's own level and KEK.
        if row.level != level.value or keys.parse_kek_id(row.kek_id).domain is not domain:
            continue
        ad_for = _text_wrap_ad(row, level)
        dek = keys.unwrap(row.kek_id, bytes(row.wrapped_dek), ad_for(row.kek_id))
        row.kek_id, row.wrapped_dek = keys.wrap(domain, dek, ad_for)
        row.save(update_fields=["kek_id", "wrapped_dek", "updated_at"])
        count += 1
    return count


@sensitive_variables()
def _rewrap_mfa(current: str, limit: int) -> int:
    from allauth.mfa.models import Authenticator

    count = 0
    for authenticator in Authenticator.objects.select_for_update().order_by("pk"):
        data = dict(authenticator.data or {})
        changed = False
        for name in MFA_SECRET_FIELDS:
            value = data.get(name)
            if not isinstance(value, str):
                continue
            try:
                used, _sealed = formats.unpack_secret(value)
            except VaultIntegrityError:
                raise VaultKeyError(
                    f"MFA authenticator {authenticator.pk}: {name} is not a vault-sealed "
                    "value (a clear secret?); seal it before rotating"
                ) from None
            if used != current:
                kek_id, sealed = keys.wrap(
                    KeyDomain.MFA_SECRETS, decrypt_secret(value).encode("utf-8"), _mfa_ad
                )
                data[name] = formats.pack_secret(kek_id, sealed)
                changed = True
        if changed:
            authenticator.data = data
            authenticator.save(update_fields=["data"])
            count += 1
            if count >= limit:
                break
    return count
