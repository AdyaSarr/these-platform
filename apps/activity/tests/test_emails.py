"""E-mails of the activity app (M11a), all through core.delivery.deliver_once:
concerned and still_due checked again at send time, a link only.

- « informations »: immediate (one e-mail per event) or a daily / weekly digest
  (one link to the feed), per the person's preference;
- three new immediate e-mails (a piece submitted -> its validators; a piece
  refused -> the student; an action assigned -> its assignee), whatever the
  preference, each with its own concerned and still_due;
- no activity e-mail for an event an existing e-mail already covers.
"""

from __future__ import annotations

import datetime as dt
import logging
import re
from types import SimpleNamespace
from typing import Any

import pytest
from django.conf import settings
from django.core import mail
from django.urls import reverse

from apps.accounts.roles import Role
from apps.activity import digests, services, tasks
from apps.activity.choices import Category, Frequency
from apps.activity.models import ActivityNotification
from apps.activity.tests import helpers as h
from apps.core.confidentiality import ConfidentialityLevel as L

pytestmark = pytest.mark.django_db(transaction=True)

LINK = re.compile(r"https?://\S+")


@pytest.fixture(autouse=True)
def _now(time_machine: Any) -> None:
    time_machine.move_to(h.NOW, tick=True)


@pytest.fixture
def team() -> SimpleNamespace:
    return h.make_team()


def _to(user: Any) -> list[Any]:
    return [m for m in mail.outbox if user.email in m.to]


def _row(user: Any, **kw: Any) -> ActivityNotification:
    row = ActivityNotification.objects.filter(recipient=user, **kw).order_by("created_at").last()
    assert row is not None
    return row


def _resend(row: ActivityNotification) -> str:
    ActivityNotification.objects.filter(pk=row.pk).update(status="PENDING", attempts=0)
    mail.outbox.clear()
    tasks.send_activity_notification(str(row.pk))
    row.refresh_from_db()
    return str(row.status)


def _immediate(team: SimpleNamespace, user: Any) -> None:
    services.set_preference(user, team.thesis, Category.INFO, Frequency.IMMEDIATE)


# --------------------------------------------------------------------------- informations


def test_an_immediate_information_is_a_link_to_the_element(team: SimpleNamespace) -> None:
    _immediate(team, team.director)
    mail.outbox.clear()
    h.document(team, L.INTERNAL_LIRMM, title="TEMOIN-FUITE-TITRE-DOCUMENT")
    event = h.events_of("DOCUMENT_ADDED")[-1]
    (message,) = _to(team.director)
    assert LINK.findall(message.body) == [
        f"{settings.SITE_URL}{h.url(team, 'open', event_id=event.pk)}"
    ]
    for text in (message.subject, message.body):
        assert "TEMOIN-FUITE" not in text and "INTERNAL" not in text


def test_the_default_supervisor_gets_no_immediate_e_mail(team: SimpleNamespace) -> None:
    mail.outbox.clear()
    h.document(team, L.INTERNAL_LIRMM)
    assert _to(team.director) == [] and _to(team.co_supervisor) == []


def test_never_an_e_mail_about_what_one_does_not_read(team: SimpleNamespace) -> None:
    _immediate(team, team.co_supervisor)
    mail.outbox.clear()
    h.document(team, L.INTERNAL_ST)
    h.meeting(team, L.INTERNAL_ST)
    h.mission(team, "ROUSSET", "ST", start=h.D(2027, 2, 9))
    assert _to(team.co_supervisor) == []
    assert not ActivityNotification.objects.filter(recipient=team.co_supervisor).exists()


def test_no_activity_e_mail_when_an_existing_one_covers_it(team: SimpleNamespace) -> None:
    """MEETING_PLANNED: the attendees already get INVITED (M5a)."""
    _immediate(team, team.director)
    h.meeting(team, L.PUBLIC, attendees=[team.student, team.director])
    assert not ActivityNotification.objects.filter(recipient=team.director).exists()


def test_concerned_at_send_time(team: SimpleNamespace) -> None:
    from apps.vault import services as vault

    _immediate(team, team.co_supervisor)
    doc = h.document(team, L.INTERNAL_LIRMM)
    row = _row(team.co_supervisor)
    vault.change_level(document=doc, level=L.INTERNAL_ST, user=team.student)
    assert _resend(row) == "SKIPPED" and mail.outbox == []


def test_a_preference_changed_since_is_respected(team: SimpleNamespace) -> None:
    _immediate(team, team.director)
    h.document(team, L.INTERNAL_LIRMM)
    row = _row(team.director)
    services.set_preference(team.director, team.thesis, Category.INFO, Frequency.FEED_ONLY)
    assert _resend(row) == "SKIPPED"


def test_already_read_is_no_longer_due(team: SimpleNamespace) -> None:
    _immediate(team, team.director)
    h.document(team, L.INTERNAL_LIRMM)
    row = _row(team.director)
    h.client_for(team.director).get(h.url(team, "open", event_id=row.event_id))
    assert _resend(row) == "SKIPPED"


def test_a_finished_thesis_sends_nothing(team: SimpleNamespace) -> None:
    from apps.accounts.models import Thesis

    _immediate(team, team.director)
    h.document(team, L.INTERNAL_LIRMM)
    row = _row(team.director)
    Thesis.objects.filter(pk=team.thesis.pk).update(status=Thesis.Status.CLOSED)
    assert _resend(row) == "SKIPPED"


def test_logs_and_broker_see_the_row_id_only(team: SimpleNamespace,
                                             caplog: pytest.LogCaptureFixture,
                                             monkeypatch: pytest.MonkeyPatch) -> None:  # fmt: skip
    sent: list[tuple[Any, ...]] = []
    monkeypatch.setattr(tasks.send_activity_notification, "delay", lambda *a: sent.append(a))
    caplog.set_level(logging.DEBUG)
    _immediate(team, team.director)
    h.document(team, L.INTERNAL_LIRMM, title="TEMOIN-FUITE-TITRE-DOCUMENT")
    row = _row(team.director)
    assert sent == [(str(row.pk),)]
    assert "TEMOIN-FUITE" not in caplog.text and team.director.email not in caplog.text


# --------------------------------------------------------------------------- digests


def test_the_daily_digest_of_the_student(team: SimpleNamespace, time_machine: Any) -> None:
    from apps.objectives import services as objectives

    goal = h.objective(team, L.PUBLIC)
    objectives.add_comment(objective=goal, user=team.director, body="Avis fictif")
    mail.outbox.clear()
    time_machine.move_to(h.NOW + dt.timedelta(days=1), tick=True)
    digests.run()
    (message,) = _to(team.student)
    assert LINK.findall(message.body)[0].endswith(h.url(team, "feed"))
    digests.run()  # twice the same day: nothing more
    assert len(_to(team.student)) == 1


def test_no_digest_without_an_unread_readable_element(team: SimpleNamespace,
                                                      time_machine: Any) -> None:  # fmt: skip
    h.document(team, L.INTERNAL_ST)  # unread by the co-supervisor, but unreadable
    services.set_preference(team.co_supervisor, team.thesis, Category.INFO, Frequency.DAILY)
    mail.outbox.clear()
    time_machine.move_to(h.NOW + dt.timedelta(days=1), tick=True)
    digests.run()
    assert _to(team.co_supervisor) == []


def test_the_weekly_digest_goes_on_monday(team: SimpleNamespace, time_machine: Any) -> None:
    h.document(team, L.INTERNAL_LIRMM)  # Wednesday 20 January
    mail.outbox.clear()
    time_machine.move_to(dt.datetime(2027, 1, 22, 6, 0, tzinfo=dt.UTC), tick=True)  # Friday
    digests.run()
    assert _to(team.director) == []
    time_machine.move_to(dt.datetime(2027, 1, 25, 6, 0, tzinfo=dt.UTC), tick=True)  # Monday
    digests.run()
    assert len(_to(team.director)) == 1


def test_a_digest_read_meanwhile_is_skipped(team: SimpleNamespace, time_machine: Any) -> None:
    h.document(team, L.INTERNAL_LIRMM)
    time_machine.move_to(dt.datetime(2027, 1, 25, 6, 0, tzinfo=dt.UTC), tick=True)
    digests.run()
    row = _row(team.director, kind="DIGEST_WEEKLY")
    h.browser_post(h.browser_for(team.director), h.url(team, "feed"),
                   h.url(team, "read_all"), {})  # fmt: skip
    assert _resend(row) == "SKIPPED"


# --------------------------------------------------------------------------- piece submitted


def test_a_submitted_piece_is_sent_to_its_validators(team: SimpleNamespace) -> None:
    services.set_preference(team.director, team.thesis, Category.INFO, Frequency.FEED_ONLY)
    mail.outbox.clear()
    piece = h.item(team, "CIFRE", [Role.DIRECTOR, Role.INDUSTRIAL_TUTOR])
    for validator in (team.director, team.tutor):
        (message,) = _to(validator)  # whatever the « informations » preference
        item_url = reverse("admin_file:item", kwargs={"thesis_id": team.thesis.pk,
                                                       "item_id": piece.pk})  # fmt: skip
        assert LINK.findall(message.body) == [f"{settings.SITE_URL}{item_url}"]
    assert _to(team.student) == [] and _to(team.co_supervisor) == []


def test_submitted_still_due_and_concerned(team: SimpleNamespace) -> None:
    from apps.accounts.models import Membership
    from apps.admin_file import services as admin_file

    piece = h.item(team, "CIFRE", [Role.DIRECTOR, Role.INDUSTRIAL_TUTOR])
    to_director, to_tutor = _row(team.director), _row(team.tutor)
    admin_file.approve(item=piece, user=team.director)
    assert _resend(to_director) == "SKIPPED"  # approved by him meanwhile
    assert _resend(to_tutor) == "SENT"  # still awaited from the tutor
    Membership.objects.filter(thesis=team.thesis, user=team.tutor).update(active_until=h.NOW)
    assert _resend(to_tutor) == "SKIPPED"  # no longer a validator


def test_submitted_is_no_longer_due_in_another_cycle(team: SimpleNamespace) -> None:
    from apps.admin_file import services as admin_file

    piece = h.item(team, "ACADEMIC", [Role.DIRECTOR])
    row = _row(team.director)
    admin_file.withdraw(item=piece, user=team.student)
    admin_file.submit(item=piece, user=team.student)  # a new cycle, a new e-mail
    assert _resend(row) == "SKIPPED"


# --------------------------------------------------------------------------- piece refused


def test_a_refused_piece_is_sent_to_the_student(team: SimpleNamespace) -> None:
    from apps.admin_file import services as admin_file

    services.set_preference(team.student, team.thesis, Category.INFO, Frequency.FEED_ONLY)
    piece = h.item(team, "ACADEMIC", [Role.DIRECTOR])
    mail.outbox.clear()
    admin_file.reject(item=piece, user=team.director, reason="ILLEGIBLE")
    (message,) = _to(team.student)
    assert "ILLEGIBLE" not in message.body and "Illisible" not in message.body
    row = _row(team.student)
    admin_file.submit(item=piece, user=team.student)
    assert _resend(row) == "SKIPPED"  # deposited again: no longer REJECTED in that cycle


# --------------------------------------------------------------------------- action assigned


def _assigned(team: SimpleNamespace, level: L = L.PUBLIC) -> Any:
    from apps.meetings import services as meetings

    session = h.meeting(team, level, days=-1, attendees=[team.student, team.director])
    mail.outbox.clear()  # the invitation of M5a: not this app's e-mail
    return meetings.add_action(meeting=session, user=team.student, title="TEMOIN-FUITE-ACTION",
                               assignee=team.director, due_date=h.D(2027, 2, 15))  # fmt: skip


def test_an_assigned_action_is_sent_to_its_assignee(team: SimpleNamespace) -> None:
    action = _assigned(team)
    (message,) = _to(team.director)
    detail = reverse("meetings:detail", kwargs={"thesis_id": team.thesis.pk,
                                                "meeting_id": action.meeting_id})  # fmt: skip
    assert LINK.findall(message.body) == [f"{settings.SITE_URL}{detail}#action-{action.pk}"]
    assert "TEMOIN-FUITE" not in message.body + message.subject


def test_assigned_concerned_and_still_due(team: SimpleNamespace) -> None:
    from apps.meetings import services as meetings
    from apps.meetings.models import ActionItem, Meeting

    action = _assigned(team)
    row = _row(team.director)
    ActionItem.objects.filter(pk=action.pk).update(assignee=team.student)
    assert _resend(row) == "SKIPPED"  # no longer assigned
    ActionItem.objects.filter(pk=action.pk).update(assignee=team.director)
    Meeting.objects.filter(pk=action.meeting_id).update(level=L.INTERNAL_ST)
    assert _resend(row) == "SKIPPED"  # the meeting's level is no longer read
    Meeting.objects.filter(pk=action.meeting_id).update(level=L.PUBLIC)
    meetings.set_action_status(action=action, user=team.director, status="DONE")
    assert _resend(row) == "SKIPPED"  # closed


# --------------------------------------------------------------------------- guard


def test_the_app_goes_through_the_shared_delivery() -> None:
    import importlib
    import inspect

    source = inspect.getsource(importlib.import_module("apps.activity.tasks"))
    assert "deliver_once(" in source and "send_mail" not in source
    notifications = importlib.import_module("apps.activity.notifications")
    for name in ("concerned", "still_due", "skip_reason"):
        assert callable(getattr(notifications, name)), name
