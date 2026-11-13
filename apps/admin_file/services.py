"""Business logic of the administrative file (M3).

Lock order (CLAUDE.md): row locks, then audit() LAST. A vault service (store,
add_version) is always called in its OWN transaction, never inside one of these.
"""

from __future__ import annotations

from django.db import transaction

from apps.accounts.models import Thesis, User
from apps.vault.services import AuditAction, audit

from . import catalog
from .models import RequiredItem


def seed_items(thesis: Thesis, *, actor: User | None = None) -> list[str]:
    """Create the catalogue items missing from ``thesis``; return their codes.

    Idempotent: an existing item (same code) is never modified, so the
    student's changes (due date...) survive a new run. Audited only when
    something was created, with the codes only (labels never reach the log).
    """
    created: list[str] = []
    with transaction.atomic():
        for entry in catalog.CATALOG:
            _item, was_created = RequiredItem.objects.get_or_create(
                thesis=thesis,
                code=entry.code,
                defaults={
                    "section": entry.section,
                    "validator_roles": list(entry.validator_roles),
                    "default_level": entry.default_level,
                    "mandatory": entry.mandatory,
                    "attachment_required": entry.attachment_required,
                    "due_date": entry.due_date(thesis),
                    "created_by": actor,
                },
            )
            if was_created:
                created.append(entry.code)
        if created:
            audit(AuditAction.ADMIN_FILE_SEEDED, actor=actor, thesis=thesis, items=created)
    return created
