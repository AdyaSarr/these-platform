"""Sign-in, sign-out and failed sign-in go to the audit log (connected in apps.py)."""

from __future__ import annotations

from typing import Any

from allauth.mfa import signals as mfa_signals
from django.contrib.auth.signals import user_logged_in, user_logged_out, user_login_failed
from django.dispatch import receiver

from apps.vault.services import AuditAction, audit


@receiver(user_logged_in, dispatch_uid="accounts.audit_login")
def _on_login(sender: Any, request: Any, user: Any, **kwargs: Any) -> None:
    audit(AuditAction.LOGIN_SUCCEEDED, actor=user, request=request)


@receiver(user_logged_out, dispatch_uid="accounts.audit_logout")
def _on_logout(sender: Any, request: Any, user: Any, **kwargs: Any) -> None:
    audit(AuditAction.LOGOUT, actor=user, request=request)


@receiver(user_login_failed, dispatch_uid="accounts.audit_login_failed")
def _on_login_failed(
    sender: Any, credentials: dict[str, Any], request: Any = None, **kwargs: Any
) -> None:
    # Django has already masked the password in ``credentials``; only the
    # identifier is used, and audit() stores it as a keyed hash.
    typed = credentials.get("email") or credentials.get("username")
    audit(
        AuditAction.LOGIN_FAILED,
        request=request,
        subject_email=typed if isinstance(typed, str) and typed else None,
    )


# --------------------------------------------------------------------------- second factor
# Only the factor's type is recorded, never its secret or the code typed.


def _mfa(action: AuditAction, request: Any, user: Any, authenticator: Any, **details: Any) -> None:
    audit(
        action, actor=user, obj=authenticator, request=request, type=authenticator.type, **details
    )


@receiver(mfa_signals.authenticator_added, dispatch_uid="accounts.audit_mfa_added")
def _on_mfa_added(sender: Any, request: Any, user: Any, authenticator: Any, **kw: Any) -> None:
    _mfa(AuditAction.MFA_FACTOR_ADDED, request, user, authenticator)


@receiver(mfa_signals.authenticator_removed, dispatch_uid="accounts.audit_mfa_removed")
def _on_mfa_removed(sender: Any, request: Any, user: Any, authenticator: Any, **kw: Any) -> None:
    _mfa(AuditAction.MFA_FACTOR_REMOVED, request, user, authenticator)


@receiver(mfa_signals.authenticator_reset, dispatch_uid="accounts.audit_mfa_reset")
def _on_mfa_reset(sender: Any, request: Any, user: Any, authenticator: Any, **kw: Any) -> None:
    _mfa(AuditAction.MFA_FACTOR_RESET, request, user, authenticator)


@receiver(mfa_signals.authenticator_used, dispatch_uid="accounts.audit_mfa_used")
def _on_mfa_used(sender: Any, request: Any, user: Any, authenticator: Any, **kw: Any) -> None:
    _mfa(
        AuditAction.MFA_FACTOR_USED,
        request,
        user,
        authenticator,
        reauthenticated=bool(kw.get("reauthenticated", False)),
    )


@receiver(mfa_signals.authentication_failed, dispatch_uid="accounts.audit_mfa_failed")
def _on_mfa_failed(sender: Any, request: Any, user: Any, authenticator: Any, **kw: Any) -> None:
    _mfa(AuditAction.MFA_FAILED, request, user, authenticator)
