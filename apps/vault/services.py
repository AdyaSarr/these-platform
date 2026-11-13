"""Vault services: the only door to encrypted documents, MFA secrets and the audit log.

Every function here follows the same rules (docs/modules/M02-coffre.md):
- rights are checked HERE (accounts.permissions), even if a view did it too; a
  refusal is audited in its own transaction, then PermissionDenied is raised;
- lock order: KEK lock (vault.keys), then row locks, then audit() LAST;
- blob before row: a failed transaction leaves an orphan blob whose DEK was
  never stored (unreadable), never a row without its blob;
- functions holding a DEK or a plaintext carry @sensitive_variables().
Re-authentication is a view concern (it needs the request).

Call each service in its OWN transaction (the default without ATOMIC_REQUESTS):
inside one enclosing transaction, the audit() of a first call would precede the
KEK lock of the next one, which vault.keys refuses (LockOrderError).
"""

from __future__ import annotations

import ipaddress
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.utils import timezone
from django.utils.text import slugify
from django.utils.translation import gettext as _
from django.views.decorators.debug import sensitive_variables

from apps.accounts.managers import UserManager
from apps.accounts.models import Thesis, User
from apps.accounts.permissions import Action, can, can_access_document, can_access_level, roles_of
from apps.core.confidentiality import ConfidentialityLevel

from . import crypto, formats, keys, locks, storage
from .errors import (
    AuditError,
    BlobNotFoundError,
    StorageError,
    UnknownKekError,
    VaultIntegrityError,
    VaultKeyError,
)
from .formats import LEVEL_TO_DOMAIN, KeyDomain
from .models import AuditEvent, Document, DocumentAccessGrant, DocumentVersion
from .validation import FILE_TYPES, validate_upload

if TYPE_CHECKING:
    from collections.abc import Mapping

    from django.contrib.auth.models import AnonymousUser
    from django.http import HttpRequest

    from .validation import Upload

__all__ = [
    "FORBIDDEN_KEY_FRAGMENTS",
    "AuditAction",
    "AuditError",
    "Opened",
    "add_version",
    "audit",
    "change_level",
    "decrypt_secret",
    "download_name",
    "encrypt_secret",
    "grant_access",
    "open_version",
    "revoke_access",
    "rewrap_batch",
    "store",
]


class AuditAction(StrEnum):
    # Authentication.
    LOGIN_SUCCEEDED = "LOGIN_SUCCEEDED"
    LOGIN_FAILED = "LOGIN_FAILED"
    LOGOUT = "LOGOUT"
    # Second factor (allauth.mfa signals).
    MFA_FACTOR_ADDED = "MFA_FACTOR_ADDED"
    MFA_FACTOR_REMOVED = "MFA_FACTOR_REMOVED"
    MFA_FACTOR_RESET = "MFA_FACTOR_RESET"  # e.g. recovery codes regenerated
    MFA_FACTOR_USED = "MFA_FACTOR_USED"  # sign-in or re-auth; type=recovery_codes included
    MFA_FAILED = "MFA_FAILED"
    # Theses and team.
    THESIS_CREATED = "THESIS_CREATED"
    INVITATION_CREATED = "INVITATION_CREATED"
    INVITATION_EMAIL_SENT = "INVITATION_EMAIL_SENT"
    INVITATION_EMAIL_FAILED = "INVITATION_EMAIL_FAILED"
    INVITATION_REVOKED = "INVITATION_REVOKED"
    INVITATION_ACCEPTED = "INVITATION_ACCEPTED"
    MEMBERSHIP_GRANTED = "MEMBERSHIP_GRANTED"
    MEMBERSHIP_EXTENDED = "MEMBERSHIP_EXTENDED"
    MEMBER_ROLE_CHANGED = "MEMBER_ROLE_CHANGED"
    MEMBER_REMOVED = "MEMBER_REMOVED"
    # Keys (M2).
    KEK_CREATED = "KEK_CREATED"
    KEK_RETIRED = "KEK_RETIRED"
    KEK_REWRAPPED = "KEK_REWRAPPED"
    # Documents (M2).
    DOCUMENT_CREATED = "DOCUMENT_CREATED"
    DOCUMENT_VERSION_ADDED = "DOCUMENT_VERSION_ADDED"
    DOCUMENT_READ = "DOCUMENT_READ"
    DOCUMENT_ACCESS_DENIED = "DOCUMENT_ACCESS_DENIED"
    DOCUMENT_LEVEL_CHANGED = "DOCUMENT_LEVEL_CHANGED"
    DOCUMENT_GRANT_ADDED = "DOCUMENT_GRANT_ADDED"
    DOCUMENT_GRANT_REVOKED = "DOCUMENT_GRANT_REVOKED"
    # Failures, kept apart so that an alert means what it says:
    DOCUMENT_INTEGRITY_FAILED = "DOCUMENT_INTEGRITY_FAILED"  # tag, AD or missing blob
    DOCUMENT_KEK_UNAVAILABLE = "DOCUMENT_KEK_UNAVAILABLE"  # retired/unknown KEK, provider off
    DOCUMENT_STORAGE_ERROR = "DOCUMENT_STORAGE_ERROR"  # storage unusable (root, unsafe file)
    # MFA secrets (M2): clear secrets sealed by `manage.py vault_encrypt_mfa_secrets`.
    MFA_SECRETS_SEALED = "MFA_SECRETS_SEALED"  # pragma: allowlist secret (an action name)


# A details key containing any of these fragments (case-insensitive) is refused,
# at any depth: the log must never hold a secret or a clear e-mail address.
# Deliberately broad ("code" also refuses "country_code"): rename the key.
FORBIDDEN_KEY_FRAGMENTS: tuple[str, ...] = (
    "password",
    "passwd",
    "pwd",
    "token",
    "secret",
    "otp",  # also totp, hotp
    "code",  # TOTP codes, recovery codes
    "recovery",
    "key",
    "credential",
    "session",
    "cookie",
    "email",  # use subject_email=, stored as a keyed hash
    "hmac",  # reserved for values computed here
)

# Canonical JSON subset of the hash chain (RFC 8785): no floats; integers within
# +-(2**53 - 1), exactly representable as IEEE 754 doubles. bool is an int: kept.
_SCALARS = (str, int, type(None))
MAX_SAFE_INTEGER = 2**53 - 1


def _check_details(value: Any, path: str = "details") -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise AuditError(f"{path}: keys must be strings")
            lowered = key.lower()
            if any(fragment in lowered for fragment in FORBIDDEN_KEY_FRAGMENTS):
                raise AuditError(f"{path}.{key}: forbidden key in audit details")
            _check_details(item, f"{path}.{key}")
    elif isinstance(value, list | tuple):
        for i, item in enumerate(value):
            _check_details(item, f"{path}[{i}]")
    elif not isinstance(value, _SCALARS):
        raise AuditError(f"{path}: {type(value).__name__} is not allowed in audit details")
    elif isinstance(value, int) and abs(value) > MAX_SAFE_INTEGER:
        raise AuditError(f"{path}: integer outside +-(2**53 - 1)")


def canonical_ip(raw: str) -> str:
    """Canonical text of an IP address, so that one address has one pseudonym.

    "2001:DB8::1" and "2001:0db8:0:0:0:0:0:1" give "2001:db8::1"; an IPv4-mapped
    IPv6 address ("::ffff:192.0.2.7") is brought back to IPv4 ("192.0.2.7").
    A value that is not an IP address is kept as is (stripped): it cannot equal
    a canonical address, so it never shares a pseudonym with one.
    """
    text = raw.strip()
    try:
        address = ipaddress.ip_address(text)
    except ValueError:
        return text
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    return str(address)


def _thesis_of(obj: object) -> Thesis | None:
    candidate = obj if isinstance(obj, Thesis) else getattr(obj, "thesis", None)
    return candidate if isinstance(candidate, Thesis) else None


def audit(
    action: AuditAction,
    *,
    actor: User | AnonymousUser | None = None,
    obj: object | None = None,
    thesis: Thesis | None = None,
    request: HttpRequest | None = None,
    subject_email: str | None = None,
    **details: Any,
) -> AuditEvent:
    """Append one event to the audit log and return it.

    ``thesis`` defaults to the object's thesis; giving both with different values
    is an error. ``actor`` may be None (failed sign-in, system task).
    ``subject_email`` (e.g. the address typed in a failed sign-in) is stored only
    as a keyed hash of its normalized form.
    """
    if not isinstance(action, AuditAction):
        raise AuditError(f"unknown audit action {action!r}")
    _check_details(details)
    deduced = _thesis_of(obj)
    if thesis is not None and deduced is not None and thesis.pk != deduced.pk:
        raise AuditError("thesis contradicts the object's thesis")
    thesis = thesis or deduced

    if subject_email is not None:
        normalized = UserManager.normalize_email(subject_email)
        details["email_hmac"] = crypto.pseudonym(normalized, purpose="email")
    json.dumps(details)  # last guard: must serialize as is

    ip = canonical_ip(request.META.get("REMOTE_ADDR") or "") if request is not None else ""
    # Last lock of the transaction: never wait for a KEK lock after this (vault.locks).
    locks.acquire_audit_lock()
    meta = getattr(obj, "_meta", None)
    return AuditEvent.objects.create(
        action=action.value,
        actor=actor if isinstance(actor, User) else None,  # AnonymousUser -> None
        thesis=thesis,
        object_type=meta.label_lower if meta is not None else "",
        object_id=str(getattr(obj, "pk", "") or ""),
        ip_hash=crypto.pseudonym(ip, purpose="ip") if ip else "",
        details_json=details,
    )


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
    *, thesis: Thesis, upload: Upload, title: str, level: ConfidentialityLevel, user: User
) -> Document:
    """Store a new document (version 1).

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
def add_version(*, document: Document, upload: Upload, user: User) -> DocumentVersion:
    """Add the next version. The version number is in the AD and is known only once
    the document row is locked: encryption and blob happen inside the transaction.
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
            audit(AuditAction.DOCUMENT_VERSION_ADDED, actor=user, obj=locked, version=number)
            return version
    raise RuntimeError("the document's level changed twice during add_version(); retry")


@sensitive_variables()
def open_version(version_id: Any, user: User) -> Opened:
    """Decrypt one version for ``user``; every read is audited.

    The version and its document are read in ONE query: the level used in the
    AD and the wrapped DEK come from the same state of the database (a
    change_level() committed meanwhile cannot pair them wrongly).
    """
    try:
        version = DocumentVersion.objects.select_related("document").filter(pk=version_id).first()
    except (ValueError, ValidationError):  # malformed id: same answer as forbidden
        version = None
    if version is None or not can_access_document(user, version.document):
        details: dict[str, Any] = {"obj": version.document} if version is not None else {}
        raise _deny(user, "read", **details)
    document = version.document
    try:
        data = _decrypt(version, document, _level(document))
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
    Documents keep their DEK and blob (only the wrapped DEK changes); MFA
    secrets are re-sealed (they have no DEK).
    """
    with transaction.atomic():
        current = keys.current_kek(domain)
        if domain is KeyDomain.MFA_SECRETS:
            count = _rewrap_mfa(current, limit)
        else:
            count = _rewrap_documents(domain, current, limit)
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
