"""Who counts as a current member for the meetings (M5a: internal roles only).

One definition for the services (attendees, assignees), the notifications (at
queue time) and the sending task (again at send time): an attendance row names a
User and outlives the membership; this is what makes it count.
"""

from __future__ import annotations

from typing import Any

from apps.accounts.models import Membership, Thesis, User
from apps.accounts.roles import Role

INTERNAL_ROLES = frozenset(
    {Role.PHD_STUDENT, Role.DIRECTOR, Role.CO_SUPERVISOR, Role.INDUSTRIAL_TUTOR}
)


def internal_member_ids(thesis_id: Any) -> set[Any]:
    """Ids of the active users holding an internal role on the thesis right now."""
    return set(
        Membership.objects.active()
        .filter(
            thesis_id=thesis_id,
            role__in=[r.value for r in INTERNAL_ROLES],
            user__is_active=True,
        )
        .values_list("user_id", flat=True)
    )


def internal_members(thesis: Thesis) -> list[User]:
    """Active users holding an internal role on ``thesis`` (attendees, assignees)."""
    ids = internal_member_ids(thesis.pk)
    return list(User.objects.filter(pk__in=ids).order_by("last_name", "first_name", "email"))


def is_internal_member(user: User, thesis_id: Any) -> bool:
    return user.is_active and user.pk in internal_member_ids(thesis_id)
