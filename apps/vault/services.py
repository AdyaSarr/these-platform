"""Vault services. M1: ``audit()`` only (see docs/modules/M02-coffre.md for the rest)."""

from __future__ import annotations

import ipaddress
import json
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from apps.accounts.managers import UserManager
from apps.accounts.models import Thesis, User

from . import crypto, locks
from .errors import AuditError
from .models import AuditEvent

if TYPE_CHECKING:
    from django.contrib.auth.models import AnonymousUser
    from django.http import HttpRequest

__all__ = ["FORBIDDEN_KEY_FRAGMENTS", "AuditAction", "AuditError", "audit"]


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

_SCALARS = (str, int, float, bool, type(None))


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
        raise AuditError(f"{path}: {type(value).__name__} is not JSON-serializable")


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
