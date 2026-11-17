"""Retention (M11a): events older than 13 months go, with their reads and their
notifications, every day (`manage.py activity_purge`); nothing more recent."""

from __future__ import annotations

import datetime as dt
from types import SimpleNamespace
from typing import Any

import pytest
from django.core.management import call_command

from apps.activity import services
from apps.activity.choices import Category, Frequency
from apps.activity.models import ActivityNotification, Event, EventRead
from apps.activity.tests import helpers as h
from apps.core.confidentiality import ConfidentialityLevel as L

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def team() -> SimpleNamespace:
    return h.make_team()


def test_thirteen_months_then_gone(team: SimpleNamespace, time_machine: Any) -> None:
    time_machine.move_to(dt.datetime(2027, 1, 20, 9, 0, tzinfo=dt.UTC), tick=False)
    services.set_preference(team.director, team.thesis, Category.INFO, Frequency.IMMEDIATE)
    h.document(team, L.INTERNAL_LIRMM, title="Ancien")
    old = h.events_of("DOCUMENT_ADDED")[-1]
    services.mark_all_read(team.director, team.thesis)
    time_machine.move_to(dt.datetime(2027, 3, 1, 9, 0, tzinfo=dt.UTC), tick=False)
    h.document(team, L.INTERNAL_LIRMM, title="Récent")
    recent = h.events_of("DOCUMENT_ADDED")[-1]

    # 2028-02-20: exactly 13 months after the old one, which stays one more day.
    time_machine.move_to(dt.datetime(2028, 2, 20, 9, 0, tzinfo=dt.UTC), tick=False)
    call_command("activity_purge")
    assert Event.objects.filter(pk=old.pk).exists()
    time_machine.move_to(dt.datetime(2028, 2, 21, 9, 0, tzinfo=dt.UTC), tick=False)
    call_command("activity_purge")
    assert not Event.objects.filter(pk=old.pk).exists()
    assert not EventRead.objects.filter(event_id=old.pk).exists()
    assert not ActivityNotification.objects.filter(event_id=old.pk).exists()
    assert Event.objects.filter(pk=recent.pk).exists()


def test_old_digests_go_too(team: SimpleNamespace, time_machine: Any) -> None:
    from apps.activity import digests

    time_machine.move_to(dt.datetime(2027, 1, 20, 9, 0, tzinfo=dt.UTC), tick=True)
    h.document(team, L.INTERNAL_LIRMM)
    time_machine.move_to(dt.datetime(2027, 1, 25, 6, 0, tzinfo=dt.UTC), tick=True)
    digests.run()
    assert ActivityNotification.objects.filter(kind="DIGEST_WEEKLY").exists()
    time_machine.move_to(dt.datetime(2028, 3, 1, 9, 0, tzinfo=dt.UTC), tick=True)
    call_command("activity_purge")
    assert not ActivityNotification.objects.exists()
