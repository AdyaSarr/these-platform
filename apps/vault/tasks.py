"""Celery tasks of the vault."""

from __future__ import annotations

from celery import shared_task
from django.core.mail import send_mail
from django.template.loader import render_to_string

from apps.accounts.models import Membership
from apps.accounts.roles import Role

from .checkpoints import format_timestamp
from .models import AuditCheckpoint


@shared_task(ignore_result=True)  # type: ignore[untyped-decorator]  # Celery is untyped
def send_checkpoint_email(seq: int) -> int:
    """Weekly copy of a checkpoint OUTSIDE the platform, to each active PhD student.

    The only e-mail with content (CLAUDE.md, rule 7 exception): exactly seq,
    hash, signature and timestamp; no thesis title, no name, no public key.
    The broker only sees the checkpoint's seq. Returns the number of e-mails.
    """
    checkpoint = AuditCheckpoint.objects.get(seq=seq)
    context = {
        "seq": checkpoint.seq,
        "hash": checkpoint.hash,
        "signature": checkpoint.signature,
        "timestamp": format_timestamp(checkpoint.timestamp),
    }
    subject = render_to_string("vault/email/checkpoint_subject.txt").strip()
    body = render_to_string("vault/email/checkpoint_body.txt", context)
    recipients = sorted(
        set(
            Membership.objects.active()
            .filter(role=Role.PHD_STUDENT, user__is_active=True)
            .values_list("user__email", flat=True)
        )
    )
    for address in recipients:  # one message each: no shared recipient list
        send_mail(subject, body, None, [address])
    return len(recipients)
