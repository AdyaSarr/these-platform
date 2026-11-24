"""Send-time checks of the export e-mails (M13b), for core.delivery.deliver_once.

As the lifecycle e-mails (M13a): sent at once, never deferred to a digest and
never subject to the preferences; an alert of an export must reach the student
even if their e-mails are grouped weekly. Open statuses: "export" (every status,
apps/accounts/statuses.py). ``concerned``: the recipient is active and still the
student of the thesis. ``still_due``: an export happened (always); an ST
agreement is still in force (not revoked).
"""

from __future__ import annotations

from typing import Any

from apps.accounts.models import Membership
from apps.accounts.roles import Role
from apps.accounts.statuses import is_open_for
from apps.core import delivery

from .models import ExportNotification

MAX_ATTEMPTS = 3
N = ExportNotification.Kind


def concerned(row: Any) -> bool:
    if not row.recipient.is_active:
        return False
    return (
        Membership.objects.active()
        .filter(user_id=row.recipient_id, thesis_id=row.thesis_id, role=Role.PHD_STUDENT)
        .exists()
    )


def still_due(row: Any) -> bool:
    if row.kind == N.EXPORTED:
        return row.export_id is not None
    if row.kind == N.ST_CONSENT_GRANTED and row.consent is not None:
        row.consent.refresh_from_db()
        return row.consent.revoked_at is None
    return False


def skip_reason(row: Any) -> str | None:
    return delivery.skip_reason(
        row,
        thesis_open=is_open_for(row.thesis, "export"),
        concerned=concerned,
        still_due=still_due,
    )
