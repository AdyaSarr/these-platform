"""Each service writes its event (M11a, acceptance criterion 1): the right kind, the
object it is about, its actor; inside the service's transaction and BEFORE its
audit() (the event is a plain insert: the lock order of CLAUDE.md holds).

transaction=True: the services chain an audited operation and vault operations.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from apps.accounts.roles import Role
from apps.activity import events
from apps.activity.models import Event
from apps.activity.tests import helpers as h
from apps.core.confidentiality import ConfidentialityLevel as L
from apps.vault.models import AuditEvent

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture(autouse=True)
def _now(time_machine: Any) -> None:
    time_machine.move_to(h.NOW, tick=True)


@pytest.fixture
def team() -> SimpleNamespace:
    return h.make_team()


@pytest.fixture
def spy(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, int]]:
    """(kind, number of audit rows) at each call of events.record."""
    calls: list[tuple[str, int]] = []
    original = events.record

    def recording(**kwargs: Any) -> Any:
        calls.append((str(kwargs["kind"]), AuditEvent.objects.count()))
        return original(**kwargs)

    monkeypatch.setattr(events, "record", recording)
    return calls


def _last(kind: str) -> Event:
    rows = h.events_of(kind)
    assert rows, kind
    event: Event = rows[-1]
    return event


def _check(team: SimpleNamespace, spy: list[tuple[str, int]], kind: str, obj: Any,
           object_type: str, actor: Any) -> None:  # fmt: skip
    event = _last(kind)
    assert (event.thesis_id, event.object_type, event.object_id, event.actor_id) == (
        team.thesis.pk, object_type, obj.pk, actor.pk,
    )  # fmt: skip
    # Written before the service's audit(): the audit table grew after the call.
    audits_at_call = [n for k, n in spy if k == kind][-1]
    assert AuditEvent.objects.count() > audits_at_call


# --------------------------------------------------------------------------- M2


def test_a_document_and_its_version(team: SimpleNamespace, spy: Any) -> None:
    from apps.vault import services as vault

    doc = h.document(team, L.INTERNAL_LIRMM)
    _check(team, spy, "DOCUMENT_ADDED", doc, "DOCUMENT", team.student)
    vault.add_version(document=doc, upload=h.pdf("v2"), user=team.student)
    _check(team, spy, "DOCUMENT_VERSION_ADDED", doc, "DOCUMENT", team.student)


def test_a_grant(team: SimpleNamespace, spy: Any) -> None:
    from apps.vault import services as vault

    doc = h.document(team, L.CONFIDENTIAL)
    grant = vault.grant_access(document=doc, grantee=team.co_supervisor, user=team.student)
    _check(team, spy, "DOCUMENT_GRANTED", grant, "DOCUMENT_GRANT", team.student)


def test_documents_linked_to_a_meeting_and_a_submission(team: SimpleNamespace, spy: Any) -> None:
    from apps.conferences import services as conferences
    from apps.meetings import services as meetings

    doc = h.document(team, L.PUBLIC)
    link = meetings.link_document(meeting=h.meeting(team, L.PUBLIC), user=team.student,
                                  document=doc)  # fmt: skip
    _check(team, spy, "DOCUMENT_LINKED", link, "MEETING_DOCUMENT", team.student)
    link2 = conferences.link_document(
        submission=h.submission(team, L.PUBLIC), user=team.student, document=doc
    )
    _check(team, spy, "DOCUMENT_LINKED", link2, "SUBMISSION_DOCUMENT", team.student)


# --------------------------------------------------------------------------- M3


def test_an_item_submitted_validated_rejected_reopened(team: SimpleNamespace, spy: Any) -> None:
    from apps.admin_file import services as admin_file

    piece = h.item(team, "ACADEMIC", [Role.DIRECTOR])
    _check(team, spy, "ITEM_SUBMITTED", piece, "ITEM", team.student)
    piece = admin_file.reject(item=piece, user=team.director, reason="ILLEGIBLE")
    _check(team, spy, "ITEM_REJECTED", piece, "ITEM", team.director)
    piece = admin_file.submit(item=piece, user=team.student)
    piece = admin_file.approve(item=piece, user=team.director)
    _check(team, spy, "ITEM_VALIDATED", piece, "ITEM", team.director)
    admin_file.reopen(item=piece, user=team.student, reason="OTHER", comment="Avenant fictif")
    _check(team, spy, "ITEM_REOPENED", piece, "ITEM", team.student)


def test_no_validated_event_while_another_validator_is_awaited(team: SimpleNamespace) -> None:
    from apps.admin_file import services as admin_file

    piece = h.item(team, "CIFRE", [Role.DIRECTOR, Role.INDUSTRIAL_TUTOR])
    admin_file.approve(item=piece, user=team.director)
    assert h.events_of("ITEM_VALIDATED") == []
    admin_file.approve(item=piece, user=team.tutor)
    assert len(h.events_of("ITEM_VALIDATED")) == 1


# --------------------------------------------------------------------------- M4


def test_objective_events(team: SimpleNamespace, spy: Any) -> None:
    from apps.objectives import services as objectives

    goal = h.objective(team, L.PUBLIC)
    _check(team, spy, "OBJECTIVE_CREATED", goal, "OBJECTIVE", team.student)
    objectives.update_objective(objective=goal, user=team.student, changes={"weight": 3},
                                reason="Ajustement fictif")  # fmt: skip
    _check(team, spy, "OBJECTIVE_CHANGED", goal, "OBJECTIVE", team.student)
    objectives.add_progress(objective=goal, user=team.student, date=h.D(2027, 1, 19),
                            progress=20, status="IN_PROGRESS")  # fmt: skip
    _check(team, spy, "PROGRESS_REPORTED", goal, "OBJECTIVE", team.student)
    comment = objectives.add_comment(objective=goal, user=team.director, body="Avis fictif")
    _check(team, spy, "OBJECTIVE_COMMENTED", comment, "OBJECTIVE_COMMENT", team.director)


# --------------------------------------------------------------------------- M5a


def test_meeting_events(team: SimpleNamespace, spy: Any) -> None:
    import datetime as dt

    from apps.meetings import services as meetings

    coming = h.meeting(team, L.PUBLIC, attendees=[team.student, team.director])
    _check(team, spy, "MEETING_PLANNED", coming, "MEETING", team.student)
    meetings.update_meeting(
        meeting=coming, user=team.student, changes={"starts_at": h.NOW + dt.timedelta(days=9)}
    )
    _check(team, spy, "MEETING_CHANGED", coming, "MEETING", team.student)
    meetings.cancel_meeting(meeting=coming, user=team.student)
    _check(team, spy, "MEETING_CANCELLED", coming, "MEETING", team.student)
    started = h.meeting(team, L.PUBLIC, days=-1, attendees=[team.student, team.director])
    action = meetings.add_action(
        meeting=started, user=team.student, title="Action fictive", assignee=team.director,
        due_date=h.D(2027, 2, 15),
    )  # fmt: skip
    _check(team, spy, "ACTION_ASSIGNED", action, "ACTION", team.student)


def test_minutes_published(team: SimpleNamespace, spy: Any, time_machine: Any) -> None:
    import datetime as dt

    from apps.meetings import services as meetings

    session = h.meeting(team, L.PUBLIC, attendees=[team.student, team.director])
    time_machine.move_to(h.NOW + dt.timedelta(days=8), tick=True)
    meetings.save_minutes(meeting=session, user=team.student, minutes="CR fictif", version=0)
    _check(team, spy, "MINUTES_PUBLISHED", session, "MEETING", team.student)


# --------------------------------------------------------------------------- M6


def test_submission_events(team: SimpleNamespace, spy: Any) -> None:
    from apps.conferences import services as conferences
    from apps.conferences.tests.helpers import article_version

    paper = h.submission(team, L.INTERNAL_ST)
    _check(team, spy, "SUBMISSION_CREATED", paper, "SUBMISSION", team.student)
    version = article_version(team, paper)
    approval = conferences.request_approval(submission=paper, user=team.student, version=version)
    _check(team, spy, "APPROVAL_REQUESTED", approval, "APPROVAL", team.student)
    conferences.decide_approval(approval=approval, user=team.tutor, decision="GRANTED",
                                recent_mfa=True)  # fmt: skip
    _check(team, spy, "APPROVAL_DECIDED", approval, "APPROVAL", team.tutor)
    conferences.set_status(submission=paper, user=team.student, status="SUBMITTED",
                           version=version)  # fmt: skip
    _check(team, spy, "SUBMISSION_STATUS_CHANGED", paper, "SUBMISSION", team.student)


# --------------------------------------------------------------------------- M7


def test_mission_events(team: SimpleNamespace, spy: Any) -> None:
    from apps.travel import services as travel

    trip = h.mission(team, "ROUSSET", "ST", start=h.D(2027, 2, 9))
    _check(team, spy, "MISSION_PLANNED", trip, "MISSION", team.student)
    travel.update_mission(mission=trip, user=team.student, changes={"end_date": h.D(2027, 2, 10)})
    _check(team, spy, "MISSION_CHANGED", trip, "MISSION", team.student)
    order = travel.request_order(mission=trip, user=team.student, estimated_total="10")
    _check(team, spy, "ORDER_REQUESTED", order, "ORDER", team.student)
    travel.decide_order(order=order, user=team.tutor, decision="GRANTED", recent_mfa=True)
    _check(team, spy, "ORDER_DECIDED", order, "ORDER", team.tutor)
    other = h.mission(team, "LIRMM", "LIRMM", start=h.D(2027, 3, 2))
    travel.cancel_mission(mission=other, user=team.student)
    _check(team, spy, "MISSION_CANCELLED", other, "MISSION", team.student)


# --------------------------------------------------------------------------- team


def test_team_events(team: SimpleNamespace, spy: Any) -> None:
    from apps.accounts import services as accounts
    from apps.accounts.models import Membership

    guest = Membership.objects.active().get(thesis=team.thesis, user=team.guest)
    changed = accounts.change_role(guest, Role.CO_SUPERVISOR, by=team.student)
    _check(team, spy, "MEMBER_ROLE_CHANGED", changed, "MEMBERSHIP", team.student)
    ended = accounts.end_membership(changed, by=team.student)
    _check(team, spy, "MEMBER_LEFT", ended, "MEMBERSHIP", team.student)
    from apps.accounts.tests.factories import OrganizationFactory, UserFactory

    newcomer = UserFactory.create()
    joined = accounts.add_membership(user=newcomer, thesis=team.thesis, role=Role.DIRECTOR,
                                     organization=OrganizationFactory.create(),
                                     created_by=team.student)  # fmt: skip
    _check(team, spy, "MEMBER_JOINED", joined, "MEMBERSHIP", newcomer)


# --------------------------------------------------------------------------- general


def test_the_set_of_kinds_is_closed() -> None:
    from apps.activity.choices import EventKind

    assert set(EventKind.values) == {
        "DOCUMENT_ADDED", "DOCUMENT_VERSION_ADDED", "DOCUMENT_GRANTED", "DOCUMENT_LINKED",
        "ITEM_SUBMITTED", "ITEM_VALIDATED", "ITEM_REJECTED", "ITEM_REOPENED",
        "OBJECTIVE_CREATED", "OBJECTIVE_CHANGED", "PROGRESS_REPORTED", "OBJECTIVE_COMMENTED",
        "MEETING_PLANNED", "MEETING_CHANGED", "MEETING_CANCELLED", "MINUTES_PUBLISHED",
        "ACTION_ASSIGNED",
        "SUBMISSION_CREATED", "SUBMISSION_STATUS_CHANGED", "APPROVAL_REQUESTED",
        "APPROVAL_DECIDED",
        "MISSION_PLANNED", "MISSION_CHANGED", "MISSION_CANCELLED", "ORDER_REQUESTED",
        "ORDER_DECIDED",
        "MEMBER_JOINED", "MEMBER_ROLE_CHANGED", "MEMBER_LEFT",
    }  # fmt: skip


def test_no_event_for_presence_days(team: SimpleNamespace) -> None:
    from apps.travel import services as travel

    travel.set_presence(thesis=team.thesis, user=team.student, first=h.D(2027, 1, 21),
                        last=h.D(2027, 1, 22), kind="LEAVE")  # fmt: skip
    assert not Event.objects.exists()


def test_a_failed_service_leaves_no_event(team: SimpleNamespace) -> None:
    """Inside the service's transaction: a refusal rolls the event back with it."""
    from django.core.exceptions import PermissionDenied

    from apps.vault import services as vault

    with pytest.raises(PermissionDenied):
        vault.store(thesis=team.thesis, upload=h.pdf(), title="Refusé", level=L.PUBLIC,
                    user=team.director)  # fmt: skip
    assert not Event.objects.exists()
