"""E-mails of the lifecycle (M13a, « Notifications », criterion 8)."""

from __future__ import annotations

import datetime as dt
import inspect
from types import SimpleNamespace
from typing import Any

import pytest
from django.core import mail

from apps.lifecycle import notifications, services, tasks
from apps.lifecycle.models import LifecycleNotification
from apps.lifecycle.tests import helpers as h

pytestmark = pytest.mark.django_db(transaction=True)
N = LifecycleNotification


@pytest.fixture
def team(time_machine: Any) -> SimpleNamespace:
    time_machine.move_to(h.NOW, tick=True)
    return h.make_team()


def _sent() -> list[tuple[str, str]]:
    return [(str(m.to[0]), str(m.body)) for m in mail.outbox]


def test_every_send_goes_through_the_shared_delivery() -> None:
    source = inspect.getsource(tasks)
    assert "deliver_once(" in source and "send_mail" not in source
    for name in ("concerned", "still_due", "skip_reason"):
        assert callable(getattr(notifications, name, None)), name


def test_the_student_is_told_of_the_declaration_by_a_link(team: SimpleNamespace) -> None:
    mail.outbox.clear()
    h.defended(team)
    ((to, body),) = _sent()
    assert to == team.student.email
    assert h.url(team, "page") in body
    assert "2027" not in body and team.thesis.title not in body  # a link only
    assert N.objects.get().status == "SENT"


def test_the_student_is_told_of_the_cancellation(team: SimpleNamespace) -> None:
    h.defended(team)
    mail.outbox.clear()
    services.cancel_defense(
        thesis=team.thesis, user=team.director, reason="wrong_date", recent_mfa=True
    )
    assert [to for to, _body in _sent()] == [team.student.email]


@pytest.mark.parametrize(("by", "told"), [("director", ["student"]), ("student", ["director"])])
def test_the_other_party_is_told_of_the_request(
    team: SimpleNamespace, time_machine: Any, by: str, told: list[str]
) -> None:
    h.defended(team)
    time_machine.move_to(h.CLOSABLE, tick=True)
    mail.outbox.clear()
    h.requested(team, getattr(team, by))
    assert [to for to, _body in _sent()] == [getattr(team, a).email for a in told]
    assert all(h.url(team, "page") in body for _to, body in _sent())


def test_every_director_is_told_of_a_student_request(team: SimpleNamespace) -> None:
    from apps.accounts.tests.factories import MembershipFactory

    other = MembershipFactory.create(thesis=team.thesis, role="DIRECTOR").user
    mail.outbox.clear()
    h.requested(team, team.student, reason="student_withdrew")
    assert sorted(to for to, _body in _sent()) == sorted([team.director.email, other.email])


def test_a_request_no_longer_open_is_not_sent(team: SimpleNamespace) -> None:
    request = h.requested(team, team.student, reason="other")
    row = N.objects.get(kind="CLOSURE_REQUESTED")
    N.objects.filter(pk=row.pk).update(status="PENDING")
    services.withdraw_closure(request=request, user=team.student)
    mail.outbox.clear()
    tasks.send_lifecycle_notification(str(row.pk))
    assert mail.outbox == []
    assert N.objects.get(pk=row.pk).status == "SKIPPED"


def test_an_expired_request_is_not_sent(team: SimpleNamespace, time_machine: Any) -> None:
    h.requested(team, team.student, reason="other")
    row = N.objects.get(kind="CLOSURE_REQUESTED")
    N.objects.filter(pk=row.pk).update(status="PENDING")
    time_machine.move_to(h.NOW + dt.timedelta(days=15), tick=True)
    mail.outbox.clear()
    tasks.send_lifecycle_notification(str(row.pk))
    assert mail.outbox == [] and N.objects.get(pk=row.pk).status == "SKIPPED"


def test_a_cancelled_defence_is_no_longer_announced(team: SimpleNamespace) -> None:
    h.defended(team)
    row = N.objects.get(kind="DEFENDED")
    N.objects.filter(pk=row.pk).update(status="PENDING")
    services.cancel_defense(
        thesis=team.thesis, user=team.director, reason="wrong_date", recent_mfa=True
    )
    mail.outbox.clear()
    tasks.send_lifecycle_notification(str(row.pk))
    assert N.objects.get(pk=row.pk).status == "SKIPPED"


def test_a_recipient_who_left_is_no_longer_concerned(team: SimpleNamespace) -> None:
    from apps.accounts.models import Membership

    h.requested(team, team.student, reason="other")
    row = N.objects.get(kind="CLOSURE_REQUESTED")
    N.objects.filter(pk=row.pk).update(status="PENDING")
    Membership.objects.filter(user=team.director).update(active_until=h.NOW)
    tasks.send_lifecycle_notification(str(row.pk))
    assert N.objects.get(pk=row.pk).status == "SKIPPED"


def test_its_own_open_statuses_are_active_and_defended(team: SimpleNamespace) -> None:
    from apps.accounts.models import Thesis

    h.defended(team)
    row = N.objects.get(kind="DEFENDED")
    assert notifications.skip_reason(row) is None  # DEFENDED: still sent
    Thesis.objects.filter(pk=team.thesis.pk).update(status=Thesis.Status.CLOSED)
    row.refresh_from_db()
    row.thesis.refresh_from_db()
    assert notifications.skip_reason(row) == "thesis_closed"


def test_a_document_added_after_the_defence_is_announced(team: SimpleNamespace) -> None:
    """M2 is open in DEFENDED (apps/accounts/statuses.py): its e-mails go too."""
    from apps.activity.models import ActivityNotification
    from apps.activity.preferences import set_preference
    from apps.activity.tests.helpers import document

    set_preference(team.director, team.thesis, "INFO", "IMMEDIATE")
    h.defended(team)
    mail.outbox.clear()
    document(team, h_level())  # the final version, say
    row = ActivityNotification.objects.get(recipient=team.director, event__kind="DOCUMENT_ADDED")
    assert row.status == "SENT" and [team.director.email] in [m.to for m in mail.outbox]


def h_level() -> Any:
    from apps.core.confidentiality import ConfidentialityLevel

    return ConfidentialityLevel.PUBLIC
