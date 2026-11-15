"""Who sees which kinds of presence day (M7, data minimisation).

- the student and the industrial tutor (the employer): every kind, leave included;
- the director: every kind, a leave day shown as "unavailable";
- a co-supervisor: LIRMM days only (any other day looks like a blank one);
- anyone else: nothing.

A user with several roles on the thesis gets the widest view. The rows are
filtered here (PresenceDay.objects.visible_to) and the counters are computed from
the filtered rows (services.split_summary): what is not fetched cannot leak.
"""

from __future__ import annotations

from enum import IntEnum
from typing import Any

from apps.accounts.models import Thesis
from apps.accounts.permissions import Action, can, roles_of
from apps.accounts.roles import Role


class PresenceAccess(IntEnum):
    """Ordered: a wider view is a larger value."""

    NONE = 0
    LIRMM_ONLY = 1
    NO_LEAVE = 2
    FULL = 3


_ROLE_ACCESS = {
    Role.PHD_STUDENT: PresenceAccess.FULL,
    Role.INDUSTRIAL_TUTOR: PresenceAccess.FULL,
    Role.DIRECTOR: PresenceAccess.NO_LEAVE,
    Role.CO_SUPERVISOR: PresenceAccess.LIRMM_ONLY,
}


def presence_access(user: Any, thesis: Thesis) -> PresenceAccess:
    if not can(user, Action.TRAVEL_READ, thesis):
        return PresenceAccess.NONE
    return max(
        (_ROLE_ACCESS.get(role, PresenceAccess.NONE) for role in roles_of(user, thesis)),
        default=PresenceAccess.NONE,
    )


def mission_document_restriction(user: Any, document: Any) -> bool:
    """Read restriction on the documents of a mission, its official mission order and
    its receipts (registered in TravelConfig.ready()): True for a document of no
    mission; else ``user`` must see the mission (a co-supervisor: LIRMM only) AND
    read the mission's level, on top of the M2 rule on the document's own level. Even
    while a level change moves the mission before its documents, a document is never
    more open than its mission."""
    from apps.accounts.permissions import can_access_level
    from apps.core.confidentiality import ConfidentialityLevel

    from .models import Mission

    mission = (
        Mission.objects.filter(official_order_document_id=document.pk).first()
        or Mission.objects.filter(expenses__receipts__document_id=document.pk).first()
    )
    if mission is None:
        return True
    thesis = document.thesis
    visible = Mission.objects.visible_to(user, thesis).filter(pk=mission.pk).exists()
    return visible and can_access_level(user, thesis, ConfidentialityLevel(mission.level))
