"""An attendee is a User; once their membership of the thesis has ended (removed by
the student, ``active_until`` passed as for a CSI member, account deactivated),
they no longer see the meeting or its texts, cannot mark the minutes, and get no
e-mail at all (notification, reminder, overdue action), even one already queued.
Their former attendance does not block the meeting either (level, duplicate)."""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any

import pytest
from django.core import mail
from django.core.exceptions import PermissionDenied
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from apps.accounts import services as accounts
from apps.accounts.models import Membership
from apps.accounts.roles import Role
from apps.meetings import reminders, services, tasks
from apps.meetings.models import Meeting, MeetingNotification
from apps.meetings.tests.helpers import START, L, make_team, meeting_of

pytestmark = pytest.mark.django_db(transaction=True)

D = dt.date


@pytest.fixture
def team() -> SimpleNamespace:
    return make_team()


def _membership(team: SimpleNamespace) -> Membership:
    return Membership.objects.active().get(thesis=team.thesis, user=team.co_supervisor)


def _removed(team: SimpleNamespace) -> None:
    accounts.end_membership(_membership(team), by=team.student)


def _expired(team: SimpleNamespace) -> None:
    # The CSI member's way out: active_until passes (no service call).
    Membership.objects.filter(pk=_membership(team).pk).update(
        active_until=timezone.now() - dt.timedelta(seconds=1)
    )


def _deactivated(team: SimpleNamespace) -> None:
    team.co_supervisor.is_active = False
    team.co_supervisor.save(update_fields=["is_active"])


ENDINGS: dict[str, Callable[[SimpleNamespace], None]] = {
    "removed": _removed,
    "expired": _expired,
    "deactivated": _deactivated,
}


@pytest.fixture(params=sorted(ENDINGS))
def ending(request: pytest.FixtureRequest) -> Callable[[SimpleNamespace], None]:
    return ENDINGS[request.param]


def _meeting_with_minutes(team: SimpleNamespace) -> Meeting:
    meeting = meeting_of(team)  # student, director, co-supervisor
    services.save_minutes(meeting=meeting, user=team.student, minutes="CR fictif", version=0)
    services.acknowledge(meeting=meeting, user=team.co_supervisor, kind="READ", version=1)
    return Meeting.objects.get(pk=meeting.pk)


def _former(team: SimpleNamespace) -> list[MeetingNotification]:
    return list(MeetingNotification.objects.filter(recipient=team.co_supervisor))


def test_a_former_attendee_no_longer_sees_the_meeting(
    team: SimpleNamespace, ending: Callable[[SimpleNamespace], None]
) -> None:
    meeting = _meeting_with_minutes(team)
    ending(team)
    user = team.co_supervisor
    with pytest.raises(PermissionDenied):
        services.meeting_detail(user, meeting)
    with pytest.raises(PermissionDenied):
        services.meeting_list(user, team.thesis, past=False)
    with pytest.raises(PermissionDenied):
        services.decision_register(user, team.thesis)
    client = Client()
    client.force_login(user)
    url = reverse("meetings:detail", kwargs={"thesis_id": team.thesis.pk, "meeting_id": meeting.pk})
    response = client.get(url)
    # A deactivated account is signed out (Django refuses it): sent to the login page.
    assert response.status_code in (302, 403)
    assert b"CR fictif" not in response.content


def test_a_former_attendee_cannot_mark_the_minutes(
    team: SimpleNamespace, ending: Callable[[SimpleNamespace], None]
) -> None:
    meeting = meeting_of(team)
    services.save_minutes(meeting=meeting, user=team.student, minutes="CR fictif", version=0)
    ending(team)
    with pytest.raises(PermissionDenied):
        services.acknowledge(meeting=meeting, user=team.co_supervisor, kind="READ", version=1)
    with pytest.raises(PermissionDenied):
        services.acknowledge(
            meeting=meeting, user=team.co_supervisor, kind="APPROVED", version=1, recent_mfa=True
        )


def test_a_former_attendee_gets_no_notification(
    team: SimpleNamespace, ending: Callable[[SimpleNamespace], None]
) -> None:
    meeting = _meeting_with_minutes(team)
    ending(team)
    before = len(_former(team))
    services.update_meeting(
        meeting=meeting, user=team.student, changes={"starts_at": START + dt.timedelta(days=7)}
    )
    services.save_minutes(meeting=meeting, user=team.student, minutes="Version 2", version=1)
    services.cancel_meeting(meeting=meeting, user=team.student)
    assert len(_former(team)) == before
    assert {r.recipient for r in MeetingNotification.objects.filter(kind="CHANGED")} == {
        team.director
    }


def test_a_former_attendee_gets_no_reminder_and_no_overdue_e_mail(
    team: SimpleNamespace, ending: Callable[[SimpleNamespace], None]
) -> None:
    meeting = meeting_of(team)
    services.add_action(
        meeting=meeting,
        user=team.student,
        title="Action fictive",
        assignee=team.co_supervisor,
        due_date=D(2027, 2, 1),
    )
    ending(team)
    before = len(_former(team))
    reminders.run(D(2027, 1, 11))  # the day before the meeting
    reminders.run(D(2027, 2, 2))  # J+1 of the action
    assert len(_former(team)) == before


def test_a_queued_e_mail_is_not_sent_after_the_end(
    team: SimpleNamespace, ending: Callable[[SimpleNamespace], None], monkeypatch: Any
) -> None:
    queued: list[str] = []
    monkeypatch.setattr(tasks.send_meeting_notification, "delay", lambda i: queued.append(i))
    meeting_of(team)
    row = MeetingNotification.objects.get(recipient=team.co_supervisor, kind="INVITED")
    ending(team)
    mail.outbox.clear()
    monkeypatch.undo()
    tasks.send_meeting_notification(str(row.pk))
    assert mail.outbox == []
    row.refresh_from_db()
    assert row.status != MeetingNotification.Status.SENT


def test_a_former_attendee_does_not_block_the_level_or_the_duplicate(
    team: SimpleNamespace, ending: Callable[[SimpleNamespace], None]
) -> None:
    meeting = meeting_of(team)
    ending(team)
    # A co-supervisor could not read CONFIDENTIAL; a former one does not count.
    services.change_level(meeting=meeting, user=team.student, level=L.CONFIDENTIAL, recent_mfa=True)
    copy = services.duplicate_meeting(
        meeting=meeting, user=team.student, starts_at=START + dt.timedelta(days=7)
    )
    assert {a.user for a in copy.attendees.all()} == {team.student, team.director}
    detail = services.meeting_detail(team.student, meeting)
    assert team.co_supervisor in detail.former_attendees
    assert team.co_supervisor not in detail.attendees


def test_a_change_of_role_keeps_the_attendance(team: SimpleNamespace) -> None:
    meeting = meeting_of(team)
    accounts.change_role(_membership(team), Role.DIRECTOR, by=team.student)
    services.update_meeting(
        meeting=meeting, user=team.student, changes={"starts_at": START + dt.timedelta(days=7)}
    )
    assert MeetingNotification.objects.filter(recipient=team.co_supervisor, kind="CHANGED").exists()
    services.meeting_detail(team.co_supervisor, meeting)


def test_the_page_lists_former_attendees_apart(team: SimpleNamespace) -> None:
    meeting = meeting_of(team)
    _removed(team)
    client = Client()
    client.force_login(team.student)
    url = reverse("meetings:detail", kwargs={"thesis_id": team.thesis.pk, "meeting_id": meeting.pk})
    html = client.get(url).content.decode()
    assert "Anciens participants" in html
    assert team.co_supervisor.get_full_name() in html.split("Anciens participants", 1)[1]
