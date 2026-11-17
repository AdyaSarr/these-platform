"""Who reads an event (readers.reads) and the SQL prefilter (readers.readable_events).

reads() reuses the existing rules only, on the CURRENT state: documents
(can_access_document, restrictions included), M3 items (section read right), M7
missions (visible_to), the team (VIEW_TEAM); for meetings, submissions and
objectives, the list's rule AND the object's level (decision of 2026-11-17,
option B: one is notified only of what one can read; the lists do not change).
Linked events follow their parent object. The actor never reads their own event.

The prefilter must give EXACTLY the set of reads() (requirement c): compared on
every role, over every type of object and every level.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from apps.accounts.roles import Role
from apps.activity.models import Event
from apps.activity.readers import readable_events, reads
from apps.activity.tests import helpers as h
from apps.core.confidentiality import ConfidentialityLevel as L

pytestmark = pytest.mark.django_db(transaction=True)

R = Role
ALL_ROLES = list(h.ROLE_ATTRS)


@pytest.fixture(autouse=True)
def _now(time_machine: Any) -> None:
    time_machine.move_to(h.NOW, tick=True)


@pytest.fixture
def team() -> SimpleNamespace:
    return h.make_team()


def _user(team: SimpleNamespace, role: Role) -> Any:
    return getattr(team, h.ROLE_ATTRS[role])


def _event(kind: str) -> Event:
    rows = h.events_of(kind)
    assert rows, kind
    return rows[-1]


def _readers(team: SimpleNamespace, event: Event) -> set[Role]:
    return {role for role in ALL_ROLES if reads(_user(team, role), event)}


# --------------------------------------------------------------------------- documents


@pytest.mark.parametrize("level", list(L))
def test_a_document_event_follows_can_access_document(team: SimpleNamespace, level: L) -> None:
    from apps.accounts.permissions import can_access_document

    doc = h.document(team, level)
    expected = {
        role for role in ALL_ROLES
        if role is not R.PHD_STUDENT and can_access_document(_user(team, role), doc)
    }  # fmt: skip
    assert _readers(team, _event("DOCUMENT_ADDED")) == expected


def test_the_actor_never_reads_their_own_event(team: SimpleNamespace) -> None:
    h.document(team, L.PUBLIC)
    assert not reads(team.student, _event("DOCUMENT_ADDED"))


def test_an_outsider_reads_nothing(team: SimpleNamespace) -> None:
    h.document(team, L.PUBLIC)
    assert not reads(team.outsider, _event("DOCUMENT_ADDED"))


def test_a_grant_is_read_by_its_grantee_only(team: SimpleNamespace) -> None:
    from apps.vault import services as vault

    doc = h.document(team, L.CONFIDENTIAL)
    vault.grant_access(document=doc, grantee=team.co_supervisor, user=team.student)
    assert _readers(team, _event("DOCUMENT_GRANTED")) == {R.CO_SUPERVISOR}


def test_an_attachment_of_the_file_stays_closed(team: SimpleNamespace) -> None:
    """The M3 restriction: an INTERNAL_ST piece is not read by the company
    co-supervisor, whom the level alone would let in."""
    from apps.admin_file.models import RequiredItem

    doc = h.document(team, L.INTERNAL_ST)
    piece = h.item(team, "CIFRE", [R.INDUSTRIAL_TUTOR])
    RequiredItem.objects.filter(pk=piece.pk).update(document=doc)
    assert R.INDUSTRIAL_CO_SUPERVISOR not in _readers(team, _event("DOCUMENT_ADDED"))


def test_the_current_state_decides(team: SimpleNamespace) -> None:
    from apps.vault import services as vault

    doc = h.document(team, L.PUBLIC)
    event = _event("DOCUMENT_ADDED")
    assert reads(team.co_supervisor, event)
    vault.change_level(document=doc, level=L.INTERNAL_ST, user=team.student)
    assert not reads(team.co_supervisor, event)


# --------------------------------------------------------------------------- option B


@pytest.mark.parametrize(
    ("make", "kind"),
    [
        (h.meeting, "MEETING_PLANNED"),
        (h.submission, "SUBMISSION_CREATED"),
        (h.objective, "OBJECTIVE_CREATED"),
    ],
    ids=["meeting", "submission", "objective"],
)
@pytest.mark.parametrize("level", list(L))
def test_the_list_rule_and_the_level(team: SimpleNamespace, make: Any, kind: str,
                                     level: L) -> None:  # fmt: skip
    from apps.accounts.permissions import LEVEL_ROLES, ROLE_ACTIONS, Action

    make(team, level)
    action = {
        "MEETING_PLANNED": Action.MEETING_READ,
        "SUBMISSION_CREATED": Action.SUBMISSION_READ,
        "OBJECTIVE_CREATED": Action.OBJECTIVE_READ,
    }[kind]
    expected = {
        role for role in ALL_ROLES
        if role is not R.PHD_STUDENT and action in ROLE_ACTIONS[role] and role in LEVEL_ROLES[level]
    }  # fmt: skip
    assert _readers(team, _event(kind)) == expected


def test_the_meetings_list_itself_does_not_change(team: SimpleNamespace) -> None:
    """Option B is the feed's rule, not the list's: the co-supervisor still sees an
    INTERNAL_ST meeting listed, with its texts restricted."""
    from apps.meetings import services as meetings

    session = h.meeting(team, L.INTERNAL_ST)
    listed = meetings.meeting_list(team.co_supervisor, team.thesis, past=False)
    assert session.pk in {row.meeting.pk for row in listed}
    assert not reads(team.co_supervisor, _event("MEETING_PLANNED"))


def test_an_action_follows_its_meeting(team: SimpleNamespace) -> None:
    from apps.meetings import services as meetings

    session = h.meeting(team, L.INTERNAL_LIRMM, attendees=[team.student, team.director])
    meetings.add_action(meeting=session, user=team.student, title="Action fictive",
                        assignee=team.director, due_date=h.D(2027, 2, 15))  # fmt: skip
    readers = _readers(team, _event("ACTION_ASSIGNED"))
    assert R.DIRECTOR in readers and R.CO_SUPERVISOR in readers
    assert R.INDUSTRIAL_TUTOR not in readers and R.INDUSTRIAL_CO_SUPERVISOR not in readers


def test_a_comment_needs_the_comment_right_and_the_level(team: SimpleNamespace) -> None:
    from apps.objectives import services as objectives

    goal = h.objective(team, L.PUBLIC)
    objectives.add_comment(objective=goal, user=team.director, body="Avis fictif")
    readers = _readers(team, _event("OBJECTIVE_COMMENTED"))
    assert R.CSI_MEMBER not in readers  # reads objectives, never their comments
    assert R.DIRECTOR not in readers  # the actor
    assert {R.CO_SUPERVISOR, R.INDUSTRIAL_TUTOR, R.INDUSTRIAL_CO_SUPERVISOR} <= readers


# --------------------------------------------------------------------------- M3, M7, team


@pytest.mark.parametrize(
    ("section", "expected"),
    [("CIFRE", {R.DIRECTOR, R.INDUSTRIAL_TUTOR}), ("ACADEMIC", {R.DIRECTOR})],
)
def test_an_item_follows_the_section_right(team: SimpleNamespace, section: str,
                                           expected: set[Role]) -> None:  # fmt: skip
    h.item(team, section, [R.DIRECTOR])
    assert _readers(team, _event("ITEM_SUBMITTED")) == expected


@pytest.mark.parametrize(
    ("destination", "payer", "expected"),
    [
        ("LIRMM", "LIRMM", {R.DIRECTOR, R.CO_SUPERVISOR, R.INDUSTRIAL_TUTOR}),
        ("ROUSSET", "ST", {R.DIRECTOR, R.INDUSTRIAL_CO_SUPERVISOR, R.INDUSTRIAL_TUTOR}),
        ("CONFERENCE", "LIRMM", {R.DIRECTOR, R.INDUSTRIAL_TUTOR}),
    ],
)
def test_a_mission_follows_visible_to(team: SimpleNamespace, destination: str, payer: str,
                                      expected: set[Role]) -> None:  # fmt: skip
    h.mission(team, destination, payer, start=h.D(2027, 2, 9))
    assert _readers(team, _event("MISSION_PLANNED")) == expected


def test_the_team_follows_view_team(team: SimpleNamespace) -> None:
    from apps.accounts import services as accounts
    from apps.accounts.models import Membership

    guest = Membership.objects.active().get(thesis=team.thesis, user=team.guest)
    accounts.change_role(guest, R.CO_SUPERVISOR, by=team.student)
    readers = _readers(team, _event("MEMBER_ROLE_CHANGED"))
    assert R.CSI_MEMBER not in readers
    assert {R.DIRECTOR, R.CO_SUPERVISOR, R.INDUSTRIAL_TUTOR,
            R.INDUSTRIAL_CO_SUPERVISOR} <= readers  # fmt: skip


# --------------------------------------------------------------------------- prefilter


def _every_kind_of_object(team: SimpleNamespace) -> None:
    """Events on every type of object, at every level, readable or not."""
    from apps.admin_file.models import RequiredItem
    from apps.meetings import services as meetings
    from apps.objectives import services as objectives
    from apps.travel import services as travel
    from apps.vault import services as vault

    for level in L:
        doc = h.document(team, level, title=f"Doc {level}")
        session = h.meeting(team, level)
        h.submission(team, level)
        goal = h.objective(team, level)
        objectives.add_comment(objective=goal, user=team.director, body="Avis fictif")
        meetings.link_document(meeting=session, user=team.student, document=doc)
    confidential = h.document(team, L.CONFIDENTIAL, title="Doc accordé")
    vault.grant_access(document=confidential, grantee=team.st_co_supervisor, user=team.student)
    attached = h.document(team, L.INTERNAL_ST, title="Pièce")
    piece = h.item(team, "CIFRE", [R.INDUSTRIAL_TUTOR])
    RequiredItem.objects.filter(pk=piece.pk).update(document=attached)
    h.item(team, "ACADEMIC", [R.DIRECTOR])
    for destination, payer, day in (("LIRMM", "LIRMM", 9), ("ROUSSET", "ST", 10),
                                    ("CONFERENCE", "LIRMM", 11)):  # fmt: skip
        trip = h.mission(team, destination, payer, start=h.D(2027, 2, day))
        order = travel.request_order(mission=trip, user=team.student, estimated_total="5")
        travel.attach_official_order(mission=trip, user=team.student, upload=h.pdf())
        assert order is not None


@pytest.mark.parametrize("role", ALL_ROLES, ids=lambda r: r.value)
def test_the_prefilter_is_exactly_reads(team: SimpleNamespace, role: Role) -> None:
    _every_kind_of_object(team)
    user = _user(team, role)
    expected = {e.pk for e in Event.objects.filter(thesis=team.thesis) if reads(user, e)}
    assert {e.pk for e in readable_events(user, team.thesis)} == expected
    if role is not R.GUEST:
        assert expected  # not vacuous


def test_the_prefilter_knows_every_document_restriction() -> None:
    """A new restriction registered by a module must be written into the prefilter
    too (or the prefilter would show a document reads() refuses)."""
    from apps.accounts import permissions

    names = {f"{r.__module__}.{r.__name__}" for r in permissions._DOCUMENT_RESTRICTIONS}
    assert names == {
        "apps.admin_file.access.attachment_restriction",
        "apps.travel.access.mission_document_restriction",
    }
