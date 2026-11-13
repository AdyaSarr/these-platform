"""Read restriction on the attachments of the administrative file.

Registered in AdminFileConfig.ready() through
accounts.permissions.register_document_restriction(): a document attached to a
RequiredItem is readable only by holders of the read right of the item's
section, on top of the M2 rule (role and level). Without it, INTERNAL_LIRMM
would open ADUM, CVEC or FSD pieces to co-supervisors through the Documents
page, and a CONFIDENTIAL academic piece to the industrial tutor.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from apps.accounts.permissions import can

from .models import SECTION_READ_ACTION, RequiredItem, Section

if TYPE_CHECKING:
    from apps.vault.models import Document


def attachment_restriction(user: Any, document: Document) -> bool:
    """True for a document attached to no item; else the section's read right."""
    section = (
        RequiredItem.objects.filter(document_id=document.pk)
        .values_list("section", flat=True)
        .first()
    )
    if section is None:
        return True
    # On the DOCUMENT's thesis: an item can only hold a document of its thesis
    # (services), and this keeps the check right even if that were broken.
    return can(user, SECTION_READ_ACTION[Section(section)], document.thesis)
