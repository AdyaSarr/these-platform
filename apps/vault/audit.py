"""Audit log: append-only, hash-chained (docs/SECURITY.md, "Journal d'audit").

Each event is a link of a chain, hashed by the FROZEN module of its format
(chain_v1.py for chain_format 1):

    hash = SHA-256(prev_hash (32 raw bytes) || canonical_json(payload))

``prev_hash`` of the first event is 32 zero bytes. ``seq`` is
assigned here, contiguous from 1, under the audit-log advisory lock (a
PostgreSQL sequence would leave gaps on rollback); the database triggers
refuse UPDATE, DELETE, TRUNCATE and any INSERT that does not extend the chain.

audit() is the LAST call of a transaction (vault.locks): it holds the
audit-log lock until commit.
"""

from __future__ import annotations

import ipaddress
import json
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

from django.db import transaction
from django.utils import timezone

from apps.accounts.managers import UserManager
from apps.accounts.models import Thesis, User

from . import chain_v1, crypto, locks
from .errors import AuditError
from .formats import MAX_SAFE_INTEGER
from .models import AuditEvent

if TYPE_CHECKING:
    from django.contrib.auth.models import AnonymousUser
    from django.http import HttpRequest

# The chain formats verification knows, by the row's chain_format ("v").
CHAIN_MODULES = MappingProxyType({chain_v1.CHAIN_FORMAT: chain_v1})
CURRENT_CHAIN = chain_v1
GENESIS_HASH = chain_v1.GENESIS_HASH
# Re-exported for the modules and tests that use them.
format_timestamp = chain_v1.format_timestamp
payload_bytes = chain_v1.payload_bytes
chain_hash = chain_v1.chain_hash


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
    # Refusal by the thesis guard (accounts.permissions), signed-in users only.
    THESIS_ACCESS_DENIED = "THESIS_ACCESS_DENIED"
    INVITATION_CREATED = "INVITATION_CREATED"
    INVITATION_EMAIL_SENT = "INVITATION_EMAIL_SENT"
    INVITATION_EMAIL_FAILED = "INVITATION_EMAIL_FAILED"
    INVITATION_REVOKED = "INVITATION_REVOKED"
    INVITATION_ACCEPTED = "INVITATION_ACCEPTED"
    MEMBERSHIP_GRANTED = "MEMBERSHIP_GRANTED"
    MEMBERSHIP_EXTENDED = "MEMBERSHIP_EXTENDED"
    MEMBER_ROLE_CHANGED = "MEMBER_ROLE_CHANGED"
    MEMBER_REMOVED = "MEMBER_REMOVED"
    # Administrative file (M3).
    ADMIN_FILE_SEEDED = "ADMIN_FILE_SEEDED"
    ADMIN_ITEM_CREATED = "ADMIN_ITEM_CREATED"  # custom item (code only, never the label)
    ADMIN_ITEM_UPDATED = "ADMIN_ITEM_UPDATED"  # due date
    ADMIN_ITEM_SUBMITTED = "ADMIN_ITEM_SUBMITTED"
    ADMIN_ITEM_WITHDRAWN = "ADMIN_ITEM_WITHDRAWN"
    ADMIN_ITEM_APPROVED = "ADMIN_ITEM_APPROVED"  # one role's approval of a cycle
    ADMIN_ITEM_VALIDATED = "ADMIN_ITEM_VALIDATED"  # every validator role approved
    ADMIN_ITEM_REJECTED = "ADMIN_ITEM_REJECTED"
    ADMIN_ITEM_REOPENED = "ADMIN_ITEM_REOPENED"
    ADMIN_ITEM_ACCESS_DENIED = "ADMIN_ITEM_ACCESS_DENIED"
    THESIS_ACTIVATED = "THESIS_ACTIVATED"  # SETUP -> ACTIVE, last mandatory item
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
    # Sealed texts (M4): never the text, only slots and object ids.
    SEALED_TEXT_READ = "SEALED_TEXT_READ"  # CONFIDENTIAL only, one event per open_texts()
    SEALED_TEXT_ACCESS_DENIED = "SEALED_TEXT_ACCESS_DENIED"
    SEALED_TEXT_INTEGRITY_FAILED = "SEALED_TEXT_INTEGRITY_FAILED"  # tag, AD or padding
    SEALED_TEXT_KEK_UNAVAILABLE = "SEALED_TEXT_KEK_UNAVAILABLE"
    # MFA secrets (M2): clear secrets sealed by `manage.py vault_encrypt_mfa_secrets`.
    MFA_SECRETS_SEALED = "MFA_SECRETS_SEALED"  # pragma: allowlist secret (an action name)
    # The log itself (M2).
    AUDIT_CHECKPOINT_CREATED = "AUDIT_CHECKPOINT_CREATED"
    AUDIT_PUBLIC_KEY_EXPORTED = "AUDIT_PUBLIC_KEY_EXPORTED"


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
# ASCII identifiers: Python's key order is then RFC 8785's (UTF-16 code units).
_DETAIL_KEY = re.compile(r"[a-z][a-z0-9_]*")


# --------------------------------------------------------------------------- validation


def check_text(value: str, path: str) -> None:
    """Every hashed text field goes through here: no NUL (PostgreSQL's jsonb and
    text refuse it, the stored row would no longer match its hash) and no lone
    surrogate (no UTF-8 encoding)."""
    if "\x00" in value:
        raise AuditError(f"{path}: NUL character")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise AuditError(f"{path}: lone surrogate") from None


def _check_details(value: Any, path: str = "details") -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str) or not _DETAIL_KEY.fullmatch(key):
                raise AuditError(f"{path}.{key!r}: key must match [a-z][a-z0-9_]*")
            if any(fragment in key for fragment in FORBIDDEN_KEY_FRAGMENTS):
                raise AuditError(f"{path}.{key}: forbidden key in audit details")
            _check_details(item, f"{path}.{key}")
    elif isinstance(value, list | tuple):
        for i, item in enumerate(value):
            _check_details(item, f"{path}[{i}]")
    elif isinstance(value, str):
        check_text(value, path)
    elif isinstance(value, bool) or value is None:
        pass
    elif isinstance(value, int):
        if abs(value) > MAX_SAFE_INTEGER:
            raise AuditError(f"{path}: integer outside +-(2**53 - 1)")
    else:  # float included: outside the canonical JSON subset
        raise AuditError(f"{path}: {type(value).__name__} is not allowed in audit details")


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


# --------------------------------------------------------------------------- chain


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
    """Append one event to the chain and return it.

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
    details = json.loads(json.dumps(details))  # what jsonb gives back (tuples -> lists)

    meta = getattr(obj, "_meta", None)
    object_type = meta.label_lower if meta is not None else ""
    object_id = str(getattr(obj, "pk", "") or "")
    for path, text in (("action", action.value), ("object_type", object_type)):
        check_text(text, path)
    check_text(object_id, "object_id")
    ip = canonical_ip(request.META.get("REMOTE_ADDR") or "") if request is not None else ""

    with transaction.atomic():
        # Last lock of the transaction (vault.locks); serializes the writers.
        locks.acquire_audit_lock()
        last = AuditEvent.objects.order_by("-seq").values_list("seq", "hash").first()
        seq, prev_hash = (last[0] + 1, last[1]) if last else (1, GENESIS_HASH)
        event = AuditEvent(
            seq=seq,
            chain_format=CURRENT_CHAIN.CHAIN_FORMAT,
            timestamp=timezone.now(),
            action=action.value,
            actor=actor if isinstance(actor, User) else None,  # AnonymousUser -> None
            thesis=thesis,
            object_type=object_type,
            object_id=object_id,
            ip_hash=crypto.pseudonym(ip, purpose="ip") if ip else "",
            details_json=details,
            prev_hash=prev_hash,
        )
        event.hash = CURRENT_CHAIN.chain_hash(prev_hash, CURRENT_CHAIN.payload_bytes(event))
        event.save(force_insert=True)
    return event


# --------------------------------------------------------------------------- verification


@dataclass(frozen=True)
class Finding:
    level: str  # "ALTERATION", "INFO" or "WARNING"
    message: str


@dataclass
class ChainReport:
    events: int = 0
    last_seq: int = 0
    findings: list[Finding] = field(default_factory=list)
    hashes: dict[int, str] = field(default_factory=dict)  # at the requested seqs

    @property
    def altered(self) -> bool:
        return any(f.level == "ALTERATION" for f in self.findings)


def verify_chain(*, keep_hashes_at: Iterable[int] = (), batch: int = 2000) -> ChainReport:
    """Recompute the whole chain from the database. Hashes are compared in
    constant time. A timestamp going back is information (clock change), not
    an alteration. Tail truncation is NOT detectable here: see checkpoints."""
    wanted = set(keep_hashes_at)
    report = ChainReport()
    expected_seq, prev_hash, previous_time = 1, GENESIS_HASH, None
    for row in AuditEvent.objects.order_by("seq").iterator(chunk_size=batch):
        if row.seq != expected_seq:
            report.findings.append(
                Finding(
                    "ALTERATION",
                    f"seq {expected_seq}: missing (expected after seq {expected_seq - 1}, "
                    f"found seq {row.seq})",
                )
            )
            prev_hash = row.prev_hash  # resynchronize: report the gap once
        if not crypto.digests_equal(row.prev_hash.encode(), prev_hash.encode()):
            report.findings.append(
                Finding(
                    "ALTERATION",
                    f"seq {row.seq}: chain broken (prev_hash does not match the hash of "
                    f"seq {row.seq - 1})",
                )
            )
        chain = CHAIN_MODULES.get(row.chain_format)
        try:
            recomputed = chain.chain_hash(row.prev_hash, chain.payload_bytes(row)) if chain else ""
        except ValueError:
            recomputed = ""
        if chain is None:
            report.findings.append(
                Finding("ALTERATION", f"seq {row.seq}: unknown chain format {row.chain_format}")
            )
        if not crypto.digests_equal(recomputed.encode(), row.hash.encode()):
            report.findings.append(
                Finding("ALTERATION", f"seq {row.seq}: hash mismatch (row modified)")
            )
        if previous_time is not None and row.timestamp < previous_time:
            report.findings.append(
                Finding("INFO", f"seq {row.seq}: timestamp earlier than seq {row.seq - 1}")
            )
        if row.seq in wanted:
            report.hashes[row.seq] = row.hash
        prev_hash, previous_time, expected_seq = row.hash, row.timestamp, row.seq + 1
        report.events += 1
        report.last_seq = row.seq
    return report
