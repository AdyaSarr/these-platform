"""Send-time checks of the lifecycle e-mails (M13a), for core.delivery.deliver_once.

Its open statuses (apps/accounts/statuses.py): ACTIVE or DEFENDED (a defended
thesis still has a closure to confirm). ``concerned``: the recipient is active
and still holds the role the e-mail is for. ``still_due``: the defence is still
declared (or still cancelled), the closure request is still open.
"""

from __future__ import annotations

from typing import Any

from apps.accounts.models import Membership, Thesis
from apps.accounts.roles import Role
from apps.accounts.statuses import is_open_for
from apps.core import delivery

from .models import LifecycleNotification

MAX_ATTEMPTS = 3
N = LifecycleNotification.Kind


def _role_for(row: Any) -> Role:
    if row.kind == N.CLOSURE_REQUESTED and row.closure_request is not None:
        requester = row.closure_request.requester_role
        return Role.DIRECTOR if requester == Role.PHD_STUDENT else Role.PHD_STUDENT
    return Role.PHD_STUDENT


def concerned(row: Any) -> bool:
    if not row.recipient.is_active:
        return False
    return (
        Membership.objects.active()
        .filter(user_id=row.recipient_id, thesis_id=row.thesis_id, role=_role_for(row))
        .exists()
    )


def still_due(row: Any) -> bool:
    thesis = Thesis.objects.get(pk=row.thesis_id)
    if row.kind == N.DEFENDED:
        return thesis.status == Thesis.Status.DEFENDED
    if row.kind == N.DEFENSE_CANCELLED:
        return thesis.status == Thesis.Status.ACTIVE and thesis.defended_on is None
    if row.kind == N.CLOSURE_REQUESTED and row.closure_request is not None:
        row.closure_request.refresh_from_db()
        return bool(row.closure_request.is_open)
    return False


def skip_reason(row: Any) -> str | None:
    return delivery.skip_reason(
        row,
        thesis_open=is_open_for(row.thesis, "lifecycle"),
        concerned=concerned,
        still_due=still_due,
    )
