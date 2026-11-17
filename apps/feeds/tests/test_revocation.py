"""Automatic revocation (M12): departure from the team, change of role, thesis
DEFENDED or CLOSED, expiry after 12 months; a notice « Échéances » 14 days before
expiry. Journaled with a code each."""

from __future__ import annotations

import datetime as dt
from types import SimpleNamespace
from typing import Any

import pytest
from django.core import mail

from apps.accounts import services as accounts
from apps.accounts.models import Membership, Thesis
from apps.accounts.roles import Role
from apps.feeds import services
from apps.feeds.tests import helpers as h
from apps.vault.models import AuditEvent

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def team(time_machine: Any) -> SimpleNamespace:
    time_machine.move_to(h.NOW, tick=True)
    return h.make_team()


def _reason() -> str:
    event = AuditEvent.objects.filter(action="CALENDAR_FEED_REVOKED").order_by("seq").last()
    assert event is not None
    return str(event.details_json["reason"])


def test_departure_revokes(team: SimpleNamespace) -> None:
    _feed, token = h.new_feed(team.director, team.thesis)
    membership = Membership.objects.active().get(thesis=team.thesis, user=team.director)
    accounts.end_membership(membership, by=team.student)
    assert h.fetch(token).status_code == 404 and _reason() == "membership_ended"


def test_a_change_of_role_revokes(team: SimpleNamespace) -> None:
    _feed, token = h.new_feed(team.co_supervisor, team.thesis)
    membership = Membership.objects.active().get(thesis=team.thesis, user=team.co_supervisor)
    accounts.change_role(membership, Role.DIRECTOR, by=team.student)
    assert h.fetch(token).status_code == 404 and _reason() == "role_changed"


@pytest.mark.parametrize("status", [Thesis.Status.DEFENDED, Thesis.Status.CLOSED])
def test_a_finished_thesis_answers_404_and_the_daily_run_revokes(team: SimpleNamespace,
                                                                 status: str) -> None:  # fmt: skip
    _feed, token = h.new_feed(team.director, team.thesis)
    Thesis.objects.filter(pk=team.thesis.pk).update(status=status)
    assert h.fetch(token).status_code == 404  # at once, before any run
    services.daily()
    assert _reason() == "thesis_closed"


def test_expiry_after_12_months(team: SimpleNamespace, time_machine: Any) -> None:
    feed, token = h.new_feed(team.director, team.thesis)
    assert feed.expires_on == dt.date(2028, 1, 20)
    time_machine.move_to(dt.datetime(2028, 1, 20, 9, 0, tzinfo=dt.UTC), tick=True)
    assert h.fetch(token).status_code == 200  # the last day
    time_machine.move_to(dt.datetime(2028, 1, 21, 9, 0, tzinfo=dt.UTC), tick=True)
    assert h.fetch(token).status_code == 404
    services.daily()
    assert _reason() == "expired"


def test_a_notice_14_days_before_expiry_once(team: SimpleNamespace, time_machine: Any) -> None:
    from apps.activity.models import Event

    h.new_feed(team.director, team.thesis)
    mail.outbox.clear()
    time_machine.move_to(dt.datetime(2028, 1, 6, 6, 0, tzinfo=dt.UTC), tick=True)  # J-14
    services.daily()
    services.daily()
    (event,) = Event.objects.filter(kind="CALENDAR_FEED_EXPIRING")
    assert event.actor_id is None
    sent = [m for m in mail.outbox if team.director.email in m.to]
    assert len(sent) == 1  # Échéances: immediate by default; a link only
    assert "/ical/" not in sent[0].body
