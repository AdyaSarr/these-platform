"""Daily reminder of the portfolio (run by `manage.py portfolio_reminders`, a systemd
timer): the next re-registration, typed by the student (PortfolioPolicy). Days are
DATES in Paris, never hours.

- J30: 8 to 30 days before the date; J7: 0 to 7 days before.
Each once per (date, window, recipient); a changed date is a new event. Theses
open for "portfolio" only (SETUP, ACTIVE: no re-registration after the defence);
the e-mail is a link (CLAUDE.md rule 7). To the student only.
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass

from django.utils import timezone

from apps.accounts.models import Thesis
from apps.accounts.statuses import open_statuses
from apps.core import timezones

from .models import PortfolioPolicy, Window
from .notifications import notify, requeue_failed, students_of

OPEN_THESIS = open_statuses("portfolio")  # apps/accounts/statuses.py
# Fixed namespace of the reminders' event ids (uuid5): never change it.
EVENT_NAMESPACE = uuid.UUID("2b9e6c4a-7f1d-4e3b-9a5c-8d0f1e2a3b4c")


@dataclass(frozen=True)
class RunResult:
    reminders: int
    requeued: int


def event(thesis_id: uuid.UUID, day: dt.date) -> uuid.UUID:
    return uuid.uuid5(EVENT_NAMESPACE, f"reregistration:{thesis_id}:{day.isoformat()}")


def window_for(days: int) -> str | None:
    if 8 <= days <= 30:
        return Window.J30
    if 0 <= days <= 7:
        return Window.J7
    return None


def run(now: dt.datetime | None = None) -> RunResult:
    if now is not None and (not isinstance(now, dt.datetime) or now.tzinfo is None):
        raise ValueError("run() needs an aware datetime")
    now = now or timezone.now()
    requeued = requeue_failed()  # earlier failures first: a new row is tried once per run
    today = timezones.local_date(now, "Europe/Paris")
    count = 0
    policies = (
        PortfolioPolicy.objects.filter(
            thesis__status__in=OPEN_THESIS, reregistration_date__isnull=False
        )
        .select_related("thesis")
        .order_by("thesis_id")
    )
    for policy in policies:
        day = policy.reregistration_date
        if day is None:  # filtered above
            continue
        window = window_for((day - today).days)
        if window is None:
            continue
        thesis: Thesis = policy.thesis
        count += notify(
            thesis, event(thesis.pk, day), students_of(thesis), window=window, due_on=day
        )
    return RunResult(count, requeued)
