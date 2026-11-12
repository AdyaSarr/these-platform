"""Sign-in, sign-out and failed sign-in go to the audit log (connected in apps.py)."""

from __future__ import annotations

from typing import Any

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
