"""Daily run of the activity app (`manage.py activity_digests`, a systemd timer at
07:00 in Paris): the digests of « informations », and FAILED e-mails queued again.

Days are DATES in Paris. A daily digest on day D covers D-1; a weekly one, on a
Monday D, covers D-7 .. D-1. A digest is queued only if the recipient has at least
one element they read and have not read yet in the period (checked again at send
time, notifications.still_due); one row per (thesis, kind, period, recipient): a
run repeated, or two runs at once, sends nothing twice.
"""

from __future__ import annotations

import datetime as dt

from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.accounts.models import Thesis

from .choices import Category, Frequency, NotificationKind
from .models import ActivityNotification
from .notifications import MAX_ATTEMPTS, _members, _queue, unread_in_period
from .preferences import frequency_of


def _due(today: dt.date) -> list[tuple[NotificationKind, Frequency, dt.date]]:
    out = [(NotificationKind.DIGEST_DAILY, Frequency.DAILY, today - dt.timedelta(days=1))]
    if today.weekday() == 0:  # Monday
        out.append((NotificationKind.DIGEST_WEEKLY, Frequency.WEEKLY, today - dt.timedelta(days=7)))
    return out


def run(today: dt.date | None = None) -> dict[str, int]:
    today = today or timezone.localdate()
    queued = again = 0
    open_theses = Thesis.objects.filter(status__in=[Thesis.Status.SETUP, Thesis.Status.ACTIVE])
    for thesis in open_theses:
        for user in _members(thesis.pk):
            frequency = frequency_of(user, thesis, Category.INFO)
            for kind, wanted, start in _due(today):
                if frequency != wanted or not unread_in_period(user, thesis, kind, start):
                    continue
                try:
                    with transaction.atomic():
                        row = ActivityNotification.objects.create(
                            thesis=thesis, recipient=user, kind=kind, period_start=start
                        )
                        _queue(row)
                except IntegrityError:
                    continue  # already queued for this period
                queued += 1
    for row in ActivityNotification.objects.filter(
        status=ActivityNotification.Status.FAILED, attempts__lt=MAX_ATTEMPTS
    ):
        ActivityNotification.objects.filter(pk=row.pk).update(
            status=ActivityNotification.Status.PENDING
        )
        with transaction.atomic():
            _queue(row)
        again += 1
    return {"queued": queued, "again": again}
