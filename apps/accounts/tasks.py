"""Celery tasks of accounts. Arguments are ids only: no secret goes through the broker."""

from __future__ import annotations

import logging
import smtplib

from celery import shared_task
from django.conf import settings
from django.core.mail import send_mail
from django.db import transaction
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import timezone

from apps.vault import crypto
from apps.vault.services import AuditAction, audit

from .models import Invitation

logger = logging.getLogger(__name__)


@shared_task(ignore_result=True)  # type: ignore[untyped-decorator]  # Celery is untyped
def send_invitation_email(invitation_id: str) -> None:
    """Generate the token, e-mail the link, then store only the token's digest.

    The e-mail goes out before the digest is saved: if saving failed, the link
    would simply not work (safe failure). If sending fails, nothing is stored and
    the team page shows the invitation as failed.
    """
    with transaction.atomic():
        inv = Invitation.objects.select_for_update().filter(pk=invitation_id).first()
        if (
            inv is None
            or inv.used_at is not None
            or inv.revoked_at is not None
            or inv.email_status != Invitation.EmailStatus.QUEUED
        ):
            return
        token = crypto.new_token()
        url = settings.SITE_URL + reverse("invitation_link", kwargs={"token": token})
        subject = render_to_string("accounts/email/invitation_subject.txt").strip()
        body = render_to_string("accounts/email/invitation_body.txt", {"url": url})
        now = timezone.now()
        try:
            send_mail(subject, body, None, [inv.email])
        except (smtplib.SMTPException, OSError):
            # The exception text may contain server details: log the type only.
            logger.warning("invitation e-mail failed (invitation %s)", inv.pk, exc_info=False)
            inv.email_status = Invitation.EmailStatus.FAILED
            inv.email_status_at = now
            inv.save(update_fields=["email_status", "email_status_at", "updated_at"])
            audit(AuditAction.INVITATION_EMAIL_FAILED, obj=inv)
            return
        inv.token_hash = crypto.hash_token(token)
        inv.expires_at = now + Invitation.VALIDITY
        inv.email_status = Invitation.EmailStatus.SENT
        inv.email_status_at = now
        inv.save(
            update_fields=[
                "token_hash",
                "expires_at",
                "email_status",
                "email_status_at",
                "updated_at",
            ]
        )
        audit(AuditAction.INVITATION_EMAIL_SENT, obj=inv)


@shared_task(ignore_result=True)  # type: ignore[untyped-decorator]  # Celery is untyped
def send_password_reset_email(user_id: str) -> None:
    """Build the reset link here, in the worker: the broker only ever sees the user id."""
    from allauth.account import app_settings as account_settings
    from allauth.account.utils import user_pk_to_url_str

    from .models import User

    user = User.objects.filter(pk=user_id, is_active=True).first()
    if user is None:
        return
    key = account_settings.PASSWORD_RESET_TOKEN_GENERATOR().make_token(user)
    path = reverse(
        "account_reset_password_from_key",
        kwargs={"uidb36": user_pk_to_url_str(user), "key": key},
    )
    url = settings.SITE_URL + path
    subject = render_to_string("accounts/email/password_reset_subject.txt").strip()
    body = render_to_string("accounts/email/password_reset_body.txt", {"url": url})
    try:
        send_mail(subject, body, None, [user.email])
    except (smtplib.SMTPException, OSError):
        logger.warning("password reset e-mail failed (user %s)", user.pk, exc_info=False)
