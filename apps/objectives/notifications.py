"""Send-time checks of the weekly summary (M11a: it goes through
core.delivery.deliver_once, as every other notification).

concerned: the recipient is still an active member holding a role the summary is
for (summary.RECIPIENT_ROLES). still_due: the summarised week still has something
for them (summary.has_news, on the objectives whose level they read now).
"""

from __future__ import annotations

from django.utils import timezone

from apps.accounts.permissions import roles_of
from apps.core import delivery

from .models import WeeklySummaryLog


def concerned(row: WeeklySummaryLog) -> bool:
    from .summary import RECIPIENT_ROLES

    user = row.recipient
    return user.is_active and bool(roles_of(user, row.thesis) & set(RECIPIENT_ROLES))


def still_due(row: WeeklySummaryLog) -> bool:
    from .summary import _active_objective_ids, has_news, window

    start, end = window(row.week_start)
    active = _active_objective_ids(row.thesis, start, end)
    return has_news(row.recipient, row.thesis, timezone.localdate(), active)


def skip_reason(row: WeeklySummaryLog) -> str | None:
    return delivery.skip_reason(
        row, thesis_open=row.thesis.is_open, concerned=concerned, still_due=still_due
    )
