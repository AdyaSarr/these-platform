"""M11b: the e-mails of the modules fall into the digests, with no loss and no
duplicate (docs/modules/M11-activite.md, « Fondre un courriel dans un résumé »).

A row is born DEFERRED when its category is at a digest frequency; the next
digest of that frequency takes every DEFERRED row of the recipient, whatever its
date, checks each one again with its module's skip_reason, links it (DIGESTED)
and never lets it be sent alone. A row queued at once whose preference changed
before sending becomes DEFERRED, never lost; back to immediate, it is released.
"""

from __future__ import annotations

import datetime as dt
from types import SimpleNamespace
from typing import Any

import pytest
from django.core import mail

from apps.accounts.roles import Role
from apps.activity import digests, services
from apps.activity.choices import Category, Frequency
from apps.activity.models import ActivityNotification
from apps.activity.tests import helpers as h
from apps.core.confidentiality import ConfidentialityLevel as L

pytestmark = pytest.mark.django_db(transaction=True)

C, F = Category, Frequency
TOMORROW = h.NOW + dt.timedelta(days=1)  # Thursday 21 January, 10:00 in Paris


@pytest.fixture(autouse=True)
def _now(time_machine: Any) -> None:
    time_machine.move_to(h.NOW, tick=True)


@pytest.fixture
def team() -> SimpleNamespace:
    return h.make_team()


def _to(user: Any) -> list[Any]:
    return [m for m in mail.outbox if user.email in m.to]


def _invitation(team: SimpleNamespace, user: Any) -> Any:
    """An M5a invitation (Échéances) to ``user``; its MeetingNotification row."""
    from apps.meetings.models import MeetingNotification

    session = h.meeting(team, L.PUBLIC, attendees=[team.student, user])
    return MeetingNotification.objects.get(meeting=session, recipient=user, kind="INVITED")


# --------------------------------------------------------------------------- born DEFERRED


def _m3_reminder(team: SimpleNamespace) -> Any:
    from apps.admin_file import reminders
    from apps.admin_file.models import ReminderLog
    from apps.admin_file.tests.factories import RequiredItemFactory

    services.set_preference(team.student, team.thesis, C.DEADLINE, F.DAILY)
    RequiredItemFactory.create(thesis=team.thesis, due_date=h.D(2027, 2, 3))  # J-14
    reminders.run(h.D(2027, 1, 20))
    return team.student, ReminderLog.objects.get(recipient=team.student)


def _m5a_invitation(team: SimpleNamespace) -> Any:
    services.set_preference(team.director, team.thesis, C.DEADLINE, F.DAILY)
    return team.director, _invitation(team, team.director)


def _m6_request(team: SimpleNamespace) -> Any:
    from apps.conferences import services as conferences
    from apps.conferences.models import ConferenceNotification
    from apps.conferences.tests.helpers import article_version

    services.set_preference(team.tutor, team.thesis, C.ACTION, F.WEEKLY)
    paper = h.submission(team, L.INTERNAL_ST)
    conferences.request_approval(
        submission=paper, user=team.student, version=article_version(team, paper)
    )
    row = ConferenceNotification.objects.get(recipient=team.tutor, kind="APPROVAL_REQUESTED")
    return team.tutor, row


def _m7_order(team: SimpleNamespace) -> Any:
    from apps.travel import services as travel
    from apps.travel.models import TravelNotification

    services.set_preference(team.tutor, team.thesis, C.ACTION, F.WEEKLY)
    trip = h.mission(team, "ROUSSET", "ST", start=h.D(2027, 2, 9))
    travel.request_order(mission=trip, user=team.student, estimated_total="10")
    return team.tutor, TravelNotification.objects.get(recipient=team.tutor, kind="ORDER_REQUESTED")


def _m11a_piece(team: SimpleNamespace) -> Any:
    services.set_preference(team.director, team.thesis, C.ACTION, F.DAILY)
    h.item(team, "ACADEMIC", [Role.DIRECTOR])
    return team.director, ActivityNotification.objects.get(recipient=team.director, kind="EVENT")


SCENARIOS = [_m3_reminder, _m5a_invitation, _m6_request, _m7_order, _m11a_piece]


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda f: f.__name__.strip("_"))
def test_a_row_is_born_deferred_and_digested_once(
    team: SimpleNamespace, scenario: Any, time_machine: Any
) -> None:
    for member in (team.student, team.director, team.tutor):  # one digest per row here
        services.set_preference(member, team.thesis, C.INFO, F.FEED_ONLY)
    mail.outbox.clear()
    user, row = scenario(team)
    assert row.status == "DEFERRED"
    assert _to(user) == []  # nothing sent alone
    time_machine.move_to(dt.datetime(2027, 1, 25, 6, 0, tzinfo=dt.UTC), tick=True)  # Monday
    digests.run()
    row.refresh_from_db()
    assert row.status == "DIGESTED" and row.digest_id is not None
    (message,) = _to(user)
    digest = ActivityNotification.objects.get(pk=row.digest_id)
    assert h.url(team, "digest", digest_id=digest.pk) in message.body
    digests.run()  # again: nothing more
    assert len(_to(user)) == 1


# --------------------------------------------------------------------------- no loss


def test_queued_at_once_then_switched_to_a_digest(
    team: SimpleNamespace, time_machine: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The element was queued while immediate; the preference became daily before
    the worker sent it: DEFERRED, then in the next digest, never lost."""
    from apps.meetings import tasks

    held: list[str] = []
    monkeypatch.setattr(tasks.send_meeting_notification, "delay", held.append)
    row = _invitation(team, team.director)
    assert row.status == "PENDING" and held == [str(row.pk)]
    services.set_preference(team.director, team.thesis, C.DEADLINE, F.DAILY)
    mail.outbox.clear()
    tasks.send_meeting_notification(str(row.pk))
    row.refresh_from_db()
    assert row.status == "DEFERRED" and mail.outbox == []
    time_machine.move_to(TOMORROW, tick=True)
    digests.run()
    row.refresh_from_db()
    assert row.status == "DIGESTED" and len(_to(team.director)) == 1


def test_back_to_immediate_releases_the_deferred_rows(team: SimpleNamespace) -> None:
    services.set_preference(team.director, team.thesis, C.DEADLINE, F.DAILY)
    row = _invitation(team, team.director)
    assert row.status == "DEFERRED"
    mail.outbox.clear()
    services.set_preference(team.director, team.thesis, C.DEADLINE, F.IMMEDIATE)
    row.refresh_from_db()
    assert row.status == "SENT" and len(_to(team.director)) == 1


def test_from_daily_to_weekly_nothing_is_left_behind(
    team: SimpleNamespace, time_machine: Any
) -> None:
    from apps.travel import services as travel
    from apps.travel.models import TravelNotification

    services.set_preference(team.tutor, team.thesis, C.ACTION, F.DAILY)
    trip = h.mission(team, "ROUSSET", "ST", start=h.D(2027, 2, 9))
    travel.request_order(mission=trip, user=team.student, estimated_total="10")
    row = TravelNotification.objects.get(recipient=team.tutor, kind="ORDER_REQUESTED")
    assert row.status == "DEFERRED"
    services.set_preference(team.tutor, team.thesis, C.ACTION, F.WEEKLY)
    time_machine.move_to(TOMORROW, tick=True)  # Thursday: no daily digest for him now
    digests.run()
    row.refresh_from_db()
    assert row.status == "DEFERRED"
    time_machine.move_to(dt.datetime(2027, 1, 25, 6, 0, tzinfo=dt.UTC), tick=True)  # Monday
    digests.run()
    row.refresh_from_db()
    assert row.status == "DIGESTED"


def test_feed_only_information_is_not_wanted(team: SimpleNamespace) -> None:
    """REOPENED is an Information: « feed only » sends nothing, logged SKIPPED."""
    from apps.admin_file import services as admin_file
    from apps.admin_file.models import ItemNotification

    services.set_preference(team.director, team.thesis, C.INFO, F.FEED_ONLY)
    piece = h.item(team, "ACADEMIC", [Role.DIRECTOR])
    piece = admin_file.approve(item=piece, user=team.director)
    mail.outbox.clear()
    admin_file.reopen(item=piece, user=team.student, reason="OTHER", comment="Avenant fictif")
    row = ItemNotification.objects.get(recipient=team.director, kind="REOPENED")
    assert row.status == "SKIPPED" and _to(team.director) == []


# --------------------------------------------------------------------------- no duplicate


def test_a_failed_row_is_not_also_digested(team: SimpleNamespace, time_machine: Any) -> None:
    from apps.meetings.models import MeetingNotification

    row = _invitation(team, team.director)
    MeetingNotification.objects.filter(pk=row.pk).update(status="FAILED", attempts=1)
    services.set_preference(team.director, team.thesis, C.DEADLINE, F.DAILY)
    time_machine.move_to(TOMORROW, tick=True)
    digests.run()
    row.refresh_from_db()
    assert row.status == "FAILED" and row.digest_id is None


@pytest.mark.serial
def test_two_simultaneous_runs_build_one_digest(team: SimpleNamespace, time_machine: Any) -> None:
    import threading

    from apps.vault.tests.concurrency import run_threads

    services.set_preference(team.director, team.thesis, C.DEADLINE, F.DAILY)
    rows = [_invitation(team, team.director) for _ in range(3)]
    time_machine.move_to(TOMORROW, tick=True)
    barrier = threading.Barrier(2, timeout=5)

    def one_run() -> None:
        barrier.wait()
        digests.run()

    mail.outbox.clear()
    run_threads(one_run, one_run, control=lambda: None)
    digest_ids = set()
    for row in rows:
        row.refresh_from_db()
        assert row.status == "DIGESTED"
        digest_ids.add(row.digest_id)
    assert len(digest_ids) == 1 and len(_to(team.director)) == 1


# --------------------------------------------------------------------------- checked again


def test_a_row_that_no_longer_holds_is_skipped_at_build(
    team: SimpleNamespace, time_machine: Any
) -> None:
    from apps.meetings import services as meetings

    services.set_preference(team.director, team.thesis, C.DEADLINE, F.DAILY)
    row = _invitation(team, team.director)
    meetings.cancel_meeting(meeting=row.meeting, user=team.student)
    mail.outbox.clear()
    time_machine.move_to(TOMORROW, tick=True)
    digests.run()
    row.refresh_from_db()
    assert row.status == "SKIPPED"  # the invitation no longer holds
    cancelled = row.meeting.notifications.get(recipient=team.director, kind="CANCELLED")
    assert cancelled.status == "DIGESTED"  # the cancellation itself does


def test_a_digest_whose_rows_fell_meanwhile_is_skipped(
    team: SimpleNamespace, time_machine: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from apps.activity import tasks
    from apps.meetings import services as meetings

    services.set_preference(team.director, team.thesis, C.DEADLINE, F.DAILY)
    row = _invitation(team, team.director)
    held: list[str] = []
    monkeypatch.setattr(tasks.send_activity_notification, "delay", held.append)
    time_machine.move_to(TOMORROW, tick=True)
    digests.run()
    meetings.cancel_meeting(meeting=row.meeting, user=team.student)
    mail.outbox.clear()
    (digest_id,) = held
    tasks.send_activity_notification(digest_id)
    assert ActivityNotification.objects.get(pk=digest_id).status == "SKIPPED"
    assert _to(team.director) == []


# --------------------------------------------------------------------------- the page


def _digest_of_director(team: SimpleNamespace, time_machine: Any) -> tuple[Any, Any]:
    services.set_preference(team.director, team.thesis, C.DEADLINE, F.DAILY)
    row = _invitation(team, team.director)
    time_machine.move_to(TOMORROW, tick=True)
    digests.run()
    row.refresh_from_db()
    return ActivityNotification.objects.get(pk=row.digest_id), row


def test_the_page_names_and_links_what_still_holds(
    team: SimpleNamespace, time_machine: Any
) -> None:
    from django.urls import reverse

    from apps.meetings import services as meetings

    digest, row = _digest_of_director(team, time_machine)
    page = h.client_for(team.director).get(h.url(team, "digest", digest_id=digest.pk))
    assert page.status_code == 200
    html = page.content.decode()
    detail = reverse("meetings:detail", kwargs={"thesis_id": team.thesis.pk,
                                                "meeting_id": row.meeting_id})  # fmt: skip
    assert detail in html and "TEMOIN-FUITE" not in html
    meetings.cancel_meeting(meeting=row.meeting, user=team.student)
    again = h.client_for(team.director).get(h.url(team, "digest", digest_id=digest.pk))
    assert detail not in again.content.decode()  # recomputed at display


@pytest.mark.parametrize("role", [r for r in h.ROLE_ATTRS if r is not Role.DIRECTOR],
                         ids=lambda r: r.value)  # fmt: skip
def test_the_page_is_its_recipients_only(
    team: SimpleNamespace, time_machine: Any, role: Role
) -> None:
    import uuid

    digest, _row = _digest_of_director(team, time_machine)
    client = h.client_for(getattr(team, h.ROLE_ATTRS[role]))
    other = client.get(h.url(team, "digest", digest_id=digest.pk))
    unknown = client.get(h.url(team, "digest", digest_id=uuid.uuid4()))
    assert other.status_code == unknown.status_code == 403
    assert other.content == unknown.content


def test_the_digest_id_is_a_random_uuid() -> None:
    import uuid

    assert ActivityNotification._meta.pk.default is uuid.uuid4
