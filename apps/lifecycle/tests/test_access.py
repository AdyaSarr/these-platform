"""Access after CLOSED (M13a, « Accès après CLOSED »): ends of membership, the
director's three months, the student kept, feeds and invitations."""

from __future__ import annotations

import datetime as dt
from types import SimpleNamespace
from typing import Any

import pytest

from apps.accounts.models import Invitation, Membership
from apps.accounts.permissions import Action, can, roles_of
from apps.lifecycle import services
from apps.lifecycle.tests import helpers as h

pytestmark = pytest.mark.django_db(transaction=True)
ENDED_AT_CLOSURE = ("co_supervisor", "st_co_supervisor", "tutor", "csi", "guest")


@pytest.fixture
def team(time_machine: Any) -> SimpleNamespace:
    time_machine.move_to(h.NOW, tick=True)
    return h.make_team()


def _active(team: SimpleNamespace, attr: str) -> bool:
    return Membership.objects.active().filter(thesis=team.thesis, user=getattr(team, attr)).exists()


def test_the_others_leave_at_the_closure(team: SimpleNamespace, time_machine: Any) -> None:
    h.closed(team, time_machine)
    for attr in ENDED_AT_CLOSURE:
        assert not _active(team, attr), attr
    assert _active(team, "student") and _active(team, "director")
    reasons = [d["reason"] for d in h.audited("MEMBER_REMOVED")]
    assert reasons == ["thesis_closed"] * len(ENDED_AT_CLOSURE)
    # Through the end of a membership (MEMBER_LEFT), never a deletion.
    assert Membership.objects.filter(thesis=team.thesis).count() == 7


def test_the_director_reads_for_three_months_then_leaves(
    team: SimpleNamespace, time_machine: Any
) -> None:
    thesis = h.closed(team, time_machine)
    assert can(team.director, Action.OBJECTIVE_READ, team.thesis)
    assert not can(team.director, Action.VALIDATE_OBJECTIVES, team.thesis)  # read only
    time_machine.move_to(dt.datetime(2027, 12, 1, 9, 0, tzinfo=dt.UTC), tick=True)
    services.daily()
    assert _active(team, "director")
    time_machine.move_to(dt.datetime(2027, 12, 2, 9, 0, tzinfo=dt.UTC), tick=True)
    counts = services.daily()
    assert counts["ended"] == 1 and thesis.access_until == h.D(2027, 12, 2)
    assert not _active(team, "director")
    assert h.audited("MEMBER_REMOVED")[-1]["reason"] == "access_period_ended"
    assert services.daily()["ended"] == 0  # once
    assert _active(team, "student")  # until M13c


def test_the_daily_run_catches_up_a_missed_end(team: SimpleNamespace, time_machine: Any) -> None:
    """A crash between the closure and the ends: the next daily run ends them."""
    from apps.accounts.models import Thesis

    h.defended(team)
    time_machine.move_to(h.CLOSABLE, tick=True)
    Thesis.objects.filter(pk=team.thesis.pk).update(
        status=Thesis.Status.CLOSED, closed_at=h.CLOSABLE, access_until=h.D(2027, 12, 2)
    )
    assert services.daily()["ended"] == len(ENDED_AT_CLOSURE)
    for attr in ENDED_AT_CLOSURE:
        assert not _active(team, attr), attr


def test_the_director_access_period_is_a_setting(
    team: SimpleNamespace, time_machine: Any, settings: Any
) -> None:
    settings.THESIS_DIRECTOR_ACCESS_MONTHS = 1
    assert h.closed(team, time_machine).access_until == h.D(2027, 10, 2)


def test_the_student_reads_after_the_closure(team: SimpleNamespace, time_machine: Any) -> None:
    h.closed(team, time_machine)
    for action in (Action.DOCUMENT_READ, Action.OBJECTIVE_READ, Action.MEETING_READ,
                   Action.TRAVEL_READ, Action.SUBMISSION_READ, Action.ADMIN_FILE_READ):  # fmt: skip
        assert can(team.student, action, team.thesis), action
    assert not can(team.student, Action.DOCUMENT_WRITE, team.thesis)


def test_the_calendar_feeds_die(team: SimpleNamespace, time_machine: Any) -> None:
    from apps.feeds import services as feeds
    from apps.feeds.models import CalendarFeed

    for attr in ("student", "director", "co_supervisor"):
        feeds.create(user=getattr(team, attr), thesis=team.thesis, recent_mfa=True)
    h.closed(team, time_machine)
    feeds.daily()
    assert not CalendarFeed.objects.filter(revoked_at__isnull=True).exists()


def test_no_one_joins_a_defended_or_closed_thesis(team: SimpleNamespace, time_machine: Any) -> None:
    from apps.accounts import services as accounts
    from apps.accounts import tasks
    from apps.accounts.tests.factories import UserFactory

    org = Membership.objects.filter(thesis=team.thesis).first().organization  # type: ignore[union-attr]
    inv = accounts.create_invitation(
        thesis=team.thesis,
        email="invite.fictif@example.org",
        role="CO_SUPERVISOR",
        organization=org,
        by=team.student,
    )
    tasks.send_invitation_email(str(inv.pk))
    h.defended(team)
    person = UserFactory.create(email="invite.fictif@example.org")
    with pytest.raises(accounts.InvalidInvitationError):  # revoked at the defence
        accounts.accept_invitation(inv.pk, user=person)
    assert not roles_of(person, team.thesis)
    assert Invitation.objects.get(pk=inv.pk).used_at is None
