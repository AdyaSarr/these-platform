"""demo_seed puts no meeting and no mission on a Saturday or a Sunday (frontend
step 2, point 13).

The scenario's dates hang on the real day (the CIFRE start is the first day of
the month five months ago; the upcoming meetings and the pending mission are a
few days ahead), so the weekday of each one changes with the day the command
runs: the seed is replayed on seven « real » days, one per weekday, in months
laid out differently.

Past dates move back to the Friday (a meeting stays before its minutes, a
mission before its return); future ones move on to the Monday (they stay to
come). A mission keeps all its days within one working week.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import pytest
import time_machine
from django.utils import timezone

from apps.demo import scenario
from apps.meetings.models import Meeting
from apps.travel.models import Mission

pytestmark = [pytest.mark.django_db(transaction=True), pytest.mark.without_vault_keys]

REAL_DAYS = [
    dt.date(2027, 1, 4),  # Monday
    dt.date(2027, 2, 2),  # Tuesday
    dt.date(2027, 3, 3),  # Wednesday
    dt.date(2027, 4, 1),  # Thursday
    dt.date(2027, 5, 7),  # Friday
    dt.date(2027, 6, 5),  # Saturday
    dt.date(2027, 7, 4),  # Sunday
]


def _days(start: dt.date, end: dt.date) -> list[dt.date]:
    return [start + dt.timedelta(days=n) for n in range((end - start).days + 1)]


@pytest.mark.parametrize("real_day", REAL_DAYS, ids=lambda d: d.strftime("%a-%Y-%m-%d"))
def test_no_meeting_nor_mission_on_a_weekend(settings: Any, real_day: dt.date) -> None:
    settings.DEBUG = True  # demo_seed refuses to run otherwise (apps/demo/guard.py)
    noon = timezone.make_aware(dt.datetime.combine(real_day, dt.time(12)))
    with time_machine.travel(noon, tick=True):
        scenario.seed()
    meetings = list(Meeting.objects.all())
    missions = list(Mission.objects.all())
    assert meetings and missions
    weekend = [
        (m.title if hasattr(m, "title") else m.pk, timezone.localtime(m.starts_at))
        for m in meetings
        if timezone.localtime(m.starts_at).weekday() >= 5
    ]
    assert weekend == []
    for mission in missions:
        days = _days(mission.start_date, mission.end_date)
        assert all(d.weekday() < 5 for d in days), (mission.start_date, mission.end_date)


@pytest.mark.parametrize(
    ("day", "back", "forward"),
    [
        (dt.date(2027, 3, 5), dt.date(2027, 3, 5), dt.date(2027, 3, 5)),  # a Friday
        (dt.date(2027, 3, 6), dt.date(2027, 3, 5), dt.date(2027, 3, 8)),  # a Saturday
        (dt.date(2027, 3, 7), dt.date(2027, 3, 5), dt.date(2027, 3, 8)),  # a Sunday
    ],
)
def test_a_day_moves_to_the_nearest_working_day(
    day: dt.date, back: dt.date, forward: dt.date
) -> None:
    assert scenario.working_day(day, back=True) == back
    assert scenario.working_day(day, back=False) == forward


@pytest.mark.parametrize(
    ("start", "length", "back", "forward"),
    [
        (dt.date(2027, 3, 1), 3, dt.date(2027, 3, 1), dt.date(2027, 3, 1)),  # Mon: fits
        (dt.date(2027, 3, 4), 3, dt.date(2027, 3, 3), dt.date(2027, 3, 8)),  # Thu..Sat
        (dt.date(2027, 3, 6), 2, dt.date(2027, 3, 4), dt.date(2027, 3, 8)),  # Sat..Sun
        (dt.date(2027, 3, 5), 1, dt.date(2027, 3, 5), dt.date(2027, 3, 5)),  # Fri alone
    ],
)
def test_a_mission_fits_in_one_working_week(
    start: dt.date, length: int, back: dt.date, forward: dt.date
) -> None:
    assert scenario.working_week_start(start, length, back=True) == back
    assert scenario.working_week_start(start, length, back=False) == forward
