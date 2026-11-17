"""The token of a personal iCal feed (M12): 256 bits, its fingerprint only in the
database, one live feed per (person, thesis), shown once; regeneration kills the
old one at once; creation and regeneration need a recent second factor,
revocation does not (a reduction of access); all three journaled, codes only."""

from __future__ import annotations

import base64
from types import SimpleNamespace
from typing import Any

import pytest
from django.core.exceptions import PermissionDenied
from django.db import IntegrityError, connection, transaction

from apps.feeds import services
from apps.feeds.models import CalendarFeed
from apps.feeds.tests import helpers as h
from apps.vault.models import AuditEvent

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def team(time_machine: Any) -> SimpleNamespace:
    time_machine.move_to(h.NOW, tick=True)
    return h.make_team()


def test_256_random_bits_and_the_fingerprint_only(team: SimpleNamespace) -> None:
    feed, token = h.new_feed(team.director, team.thesis)
    assert len(base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))) == 32
    from apps.vault.crypto import hash_token

    assert feed.token_hash == hash_token(token)
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname = 'public' ORDER BY tablename"
        )
        tables = [row[0] for row in cursor.fetchall()]
        for table in tables:
            cursor.execute(f'SELECT CAST(t AS text) FROM "{table}" t')  # noqa: S608
            assert not any(token in row[0] for row in cursor.fetchall()), table


def test_one_live_feed_per_person_and_thesis(team: SimpleNamespace) -> None:
    feed, _token = h.new_feed(team.director, team.thesis)
    with pytest.raises(IntegrityError), transaction.atomic():
        CalendarFeed.objects.create(
            user=team.director, thesis=team.thesis, token_hash="0" * 64,
            expires_on=feed.expires_on,
        )  # fmt: skip
    with pytest.raises(ValueError):  # the service says it before the database
        h.new_feed(team.director, team.thesis)


def test_creation_and_regeneration_need_a_recent_second_factor(team: SimpleNamespace) -> None:
    with pytest.raises(PermissionDenied):
        services.create(user=team.director, thesis=team.thesis, recent_mfa=False)
    h.new_feed(team.director, team.thesis)
    with pytest.raises(PermissionDenied):
        services.regenerate(user=team.director, thesis=team.thesis, recent_mfa=False)


def test_regeneration_kills_the_old_token_at_once(team: SimpleNamespace) -> None:
    _feed, old = h.new_feed(team.director, team.thesis)
    assert h.fetch(old).status_code == 200
    _feed, new = services.regenerate(user=team.director, thesis=team.thesis, recent_mfa=True)
    assert h.fetch(old).status_code == 404
    assert h.fetch(new).status_code == 200


def test_revocation_needs_no_second_factor(team: SimpleNamespace) -> None:
    _feed, token = h.new_feed(team.director, team.thesis)
    services.revoke(user=team.director, thesis=team.thesis)
    assert h.fetch(token).status_code == 404


def test_journal_holds_codes_only(team: SimpleNamespace) -> None:
    feed, token = h.new_feed(team.director, team.thesis)
    services.regenerate(user=team.director, thesis=team.thesis, recent_mfa=True)
    services.revoke(user=team.director, thesis=team.thesis)
    events = AuditEvent.objects.filter(action__startswith="CALENDAR_FEED_").order_by("seq")
    assert [e.action for e in events] == [
        "CALENDAR_FEED_CREATED", "CALENDAR_FEED_REGENERATED", "CALENDAR_FEED_REVOKED",
    ]  # fmt: skip
    assert events.last().details_json == {"reason": "user"}
    for event in events:
        text = str(event.details_json)
        assert token not in text and feed.token_hash not in text


@pytest.mark.parametrize("role", ["csi", "guest", "outsider"])
def test_excluded_roles_get_no_feed(team: SimpleNamespace, role: str) -> None:
    with pytest.raises(PermissionDenied):
        services.create(user=getattr(team, role), thesis=team.thesis, recent_mfa=True)


@pytest.mark.parametrize("role", h.ALLOWED_ROLES)
def test_allowed_roles_get_a_feed(team: SimpleNamespace, role: str) -> None:
    _feed, token = h.new_feed(getattr(team, role), team.thesis)
    assert h.fetch(token).status_code == 200


def test_last_used_is_the_day_and_written_once_a_day(team: SimpleNamespace,
                                                     time_machine: Any) -> None:  # fmt: skip
    import datetime as dt

    feed, token = h.new_feed(team.director, team.thesis)
    assert feed.last_used_on is None
    h.fetch(token)
    feed.refresh_from_db()
    assert feed.last_used_on == h.NOW.date()
    stamp = feed.updated_at
    h.fetch(token)
    feed.refresh_from_db()
    assert feed.updated_at == stamp  # the same day: not written again
    time_machine.move_to(h.NOW + dt.timedelta(days=1), tick=True)
    h.fetch(token)
    feed.refresh_from_db()
    assert feed.last_used_on == (h.NOW + dt.timedelta(days=1)).date()
