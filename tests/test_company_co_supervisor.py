"""The company co-supervisor (INDUSTRIAL_CO_SUPERVISOR) across the modules: for
each module, what the role may do and what it is refused; the refusals look like
any other; several holders of one role on a thesis work (two tutors, two LIRMM
co-supervisors).

Rights (docs/ARCHITECTURE.md): those of the LIRMM co-supervisor, on the company
side. Levels PUBLIC and INTERNAL_ST, never INTERNAL_LIRMM; CONFIDENTIAL through an
explicit grant only (the M2 rule of the co-supervisor); no administrative file
(M3), no ST approval (M6); travel: ST_ONLY (apps/travel/tests/test_st_only.py).

transaction=True (CLAUDE.md): signing in is audited, then the vault stores.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any

import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client
from django.urls import reverse

from apps.accounts.models import Membership
from apps.accounts.permissions import can_access_document
from apps.accounts.roles import Role
from apps.accounts.tests.factories import MembershipFactory
from apps.admin_file import services as admin_file
from apps.admin_file.models import ItemStatus, RequiredItem, Section
from apps.admin_file.services import seed_items
from apps.admin_file.tests.helpers import item_of, make_team, put
from apps.conferences import services as conferences
from apps.conferences.tests.helpers import article_version, submission_of
from apps.core.confidentiality import ConfidentialityLevel as L
from apps.meetings.members import INTERNAL_ROLES, is_internal_member
from apps.meetings.tests.helpers import meeting_of
from apps.travel import services as travel
from apps.travel.tests.helpers import mission_of
from apps.vault import services as vault
from apps.vault.models import AuditEvent, Document

pytestmark = pytest.mark.django_db(transaction=True)

ICS = Role.INDUSTRIAL_CO_SUPERVISOR


@pytest.fixture
def team() -> SimpleNamespace:
    team = make_team(mfa=True)  # the pages need a second factor (MFA middleware)
    team.ics = MembershipFactory.create(thesis=team.thesis, role=ICS, user__mfa=True).user
    return team


def _client(user: Any) -> Client:
    client = Client()
    client.force_login(user)
    return client


def _url(team: SimpleNamespace, name: str, **kw: Any) -> str:
    return reverse(name, kwargs={"thesis_id": team.thesis.pk, **kw})


def _store(team: SimpleNamespace, level: L, title: str = "Note fictive") -> Document:
    return vault.store(
        thesis=team.thesis,
        upload=SimpleUploadedFile("note.pdf", b"%PDF-1.7\n%note fictive\n%%EOF\n"),
        title=title,
        level=level,
        user=team.student,
    )


# --------------------------------------------------------------------------- team, objectives


def test_the_team_page_is_read_and_the_invitation_refused(team: SimpleNamespace) -> None:
    client = _client(team.ics)
    page = client.get(_url(team, "accounts:team"))
    assert page.status_code == 200
    assert "Co-encadrant·e entreprise" in page.content.decode()
    assert client.get(_url(team, "accounts:team_invite")).status_code == 403


def test_the_objectives_are_read(team: SimpleNamespace) -> None:
    assert _client(team.ics).get(_url(team, "objectives:dashboard")).status_code == 200


# --------------------------------------------------------------------------- documents


def test_levels_public_and_internal_st_only(team: SimpleNamespace) -> None:
    readable = {lv: _store(team, lv) for lv in (L.PUBLIC, L.INTERNAL_ST)}
    hidden = {lv: _store(team, lv) for lv in (L.INTERNAL_LIRMM, L.CONFIDENTIAL)}
    for document in readable.values():
        assert can_access_document(team.ics, document)
    for document in hidden.values():
        assert not can_access_document(team.ics, document)
    listed = _client(team.ics).get(_url(team, "vault:documents"))
    assert listed.status_code == 200
    ids = {d.pk for d in listed.context["documents"]}
    assert ids == {d.pk for d in readable.values()}


def test_a_hidden_document_looks_like_an_unknown_one(team: SimpleNamespace) -> None:
    hidden = _store(team, L.INTERNAL_LIRMM)
    client = _client(team.ics)
    seen = client.get(_url(team, "vault:document", document_id=hidden.pk))
    unknown = client.get(_url(team, "vault:document", document_id=uuid.uuid4()))
    assert seen.status_code == unknown.status_code != 200


def test_confidential_only_through_an_explicit_grant(team: SimpleNamespace) -> None:
    """The rule of the LIRMM co-supervisor (permissions.LEVEL_ROLES[CONFIDENTIAL])."""
    document = _store(team, L.CONFIDENTIAL)
    assert not can_access_document(team.ics, document)
    vault.grant_access(document=document, grantee=team.ics, user=team.student)
    assert can_access_document(team.ics, document)
    assert can_access_document(team.co_supervisor, document) is False  # not granted


# --------------------------------------------------------------------------- M3


def _attach(team: SimpleNamespace, code: str, level: L | None = None) -> Document:
    item = RequiredItem.objects.get(thesis=team.thesis, code=code)
    document = _store(team, level or L(item.default_level), "Pièce fictive")
    RequiredItem.objects.filter(pk=item.pk).update(document=document)
    return document


def test_no_administrative_file(team: SimpleNamespace) -> None:
    client = _client(team.ics)
    assert client.get(_url(team, "admin_file:file")).status_code == 403
    dashboard = client.get(_url(team, "accounts:dashboard")).content.decode()
    assert 'data-block="admin_file"' not in dashboard
    tutor = _client(team.tutor).get(_url(team, "accounts:dashboard")).content.decode()
    assert 'data-block="admin_file"' in tutor  # not vacuous


def test_an_attachment_stays_closed_even_at_internal_st_or_granted(team: SimpleNamespace) -> None:
    seed_items(team.thesis)
    contract = _attach(team, "employment_contract")  # INTERNAL_ST: the salary
    assert contract.confidentiality == L.INTERNAL_ST
    assert not can_access_document(team.ics, contract)
    assert can_access_document(team.tutor, contract)  # not vacuous
    confidential = _attach(team, "collaboration_contract", L.CONFIDENTIAL)
    vault.grant_access(document=confidential, grantee=team.ics, user=team.student)
    assert not can_access_document(team.ics, confidential)


def test_never_a_validator(team: SimpleNamespace) -> None:
    item = put(item_of(team, validator_roles=[Role.INDUSTRIAL_TUTOR]), "SUBMITTED", team.student)
    with pytest.raises(PermissionDenied):
        admin_file.approve(item=item, user=team.ics)


def test_the_refusal_looks_like_an_unknown_thesis(team: SimpleNamespace) -> None:
    client = _client(team.ics)
    refused = client.get(_url(team, "admin_file:file"))
    unknown = client.get(reverse("admin_file:file", kwargs={"thesis_id": uuid.uuid4()}))
    assert refused.status_code == unknown.status_code == 403
    assert refused.content == unknown.content
    reasons = [
        e.details_json.get("reason")
        for e in AuditEvent.objects.filter(action="THESIS_ACCESS_DENIED", actor=team.ics)
    ]
    assert sorted(reasons) == ["missing_right", "unknown_thesis"]


# --------------------------------------------------------------------------- M5a


def test_an_internal_member_who_attends_internal_st_meetings(team: SimpleNamespace) -> None:
    assert ICS in INTERNAL_ROLES
    assert is_internal_member(team.ics, team.thesis.pk)
    meeting = meeting_of(team, level=L.INTERNAL_ST, attendees=[team.student, team.ics])
    detail = _url(team, "meetings:detail", meeting_id=meeting.pk)
    assert _client(team.ics).get(detail).status_code == 200


def test_an_internal_lirmm_meeting_stays_restricted(team: SimpleNamespace) -> None:
    """The M5a rule of a reader without the level (as the LIRMM co-supervisor before
    an INTERNAL_ST meeting): the meeting is listed, its texts are restricted."""
    title = "TEMOIN-FUITE-REUNION-LABO"
    meeting = meeting_of(
        team, level=L.INTERNAL_LIRMM, title=title, attendees=[team.student, team.director]
    )
    client = _client(team.ics)
    detail = client.get(_url(team, "meetings:detail", meeting_id=meeting.pk)).content.decode()
    listing = client.get(_url(team, "meetings:list")).content.decode()
    for html in (detail, listing):
        assert title not in html and "Contenu restreint" in html
    with pytest.raises((ValidationError, PermissionDenied)):  # never an attendee of it
        meeting_of(team, level=L.INTERNAL_LIRMM, attendees=[team.student, team.ics])


# --------------------------------------------------------------------------- M6


def test_conferences_read_and_written_but_no_st_approval(team: SimpleNamespace) -> None:
    client = _client(team.ics)
    assert client.get(_url(team, "conferences:deadlines")).status_code == 200
    submission = submission_of(team, level=L.INTERNAL_ST)
    version = article_version(team, submission)
    approval = conferences.request_approval(
        submission=submission, user=team.student, version=version
    )
    assert conferences.pending_approvals(team.ics, team.thesis) == []
    with pytest.raises(PermissionDenied):
        conferences.decide_approval(
            approval=approval, user=team.ics, decision="GRANTED", recent_mfa=True
        )
    with pytest.raises(PermissionDenied):
        conferences.set_st_work(submission=submission, user=team.ics, value=False, recent_mfa=True)
    with pytest.raises(PermissionDenied):
        conferences.set_review_days(thesis=team.thesis, user=team.ics, days=20, recent_mfa=True)


# --------------------------------------------------------------------------- several holders


def test_two_tutors_one_decides_for_both(team: SimpleNamespace) -> None:
    """M7: both tutors see the ST approval to decide; once one decides, it leaves the
    other's queue."""
    second = MembershipFactory.create(
        thesis=team.thesis, role=Role.INDUSTRIAL_TUTOR, user__mfa=False
    ).user
    mission = mission_of(team, destination="ROUSSET", payer="ST")
    order = travel.request_order(mission=mission, user=team.student, estimated_total="1")
    assert [o.pk for o in travel.pending_orders_for(team.tutor, team.thesis)] == [order.pk]
    assert [o.pk for o in travel.pending_orders_for(second, team.thesis)] == [order.pk]
    travel.decide_order(order=order, user=second, decision="GRANTED", recent_mfa=True)
    assert travel.pending_orders_for(team.tutor, team.thesis) == []


def test_m3_validation_by_either_of_two_tutors(team: SimpleNamespace) -> None:
    second = MembershipFactory.create(
        thesis=team.thesis, role=Role.INDUSTRIAL_TUTOR, user__mfa=False
    ).user
    alone = put(
        item_of(team, section=Section.CIFRE, validator_roles=[Role.INDUSTRIAL_TUTOR]),
        ItemStatus.SUBMITTED,
        team.student,
    )
    assert admin_file.approve(item=alone, user=second).status == ItemStatus.VALIDATED
    both = put(
        item_of(
            team,
            code="collaboration_contract",
            section=Section.CIFRE,
            validator_roles=[Role.DIRECTOR, Role.INDUSTRIAL_TUTOR],
        ),
        ItemStatus.SUBMITTED,
        team.student,
    )
    assert admin_file.approve(item=both, user=second).status == ItemStatus.SUBMITTED
    with pytest.raises(PermissionDenied):  # the tutor role has approved this cycle
        admin_file.approve(item=both, user=team.tutor)
    assert admin_file.approve(item=both, user=team.director).status == ItemStatus.VALIDATED


def test_two_lirmm_co_supervisors_see_the_same(team: SimpleNamespace) -> None:
    second = MembershipFactory.create(
        thesis=team.thesis, role=Role.CO_SUPERVISOR, user__mfa=False
    ).user
    mission_of(team, destination="LIRMM", payer="LIRMM")
    mission_of(team, destination="ROUSSET", payer="ST")
    first_list = {m.pk for m in travel.mission_list(team.co_supervisor, team.thesis)}
    assert first_list and first_list == {m.pk for m in travel.mission_list(second, team.thesis)}
    holders = Membership.objects.active().filter(thesis=team.thesis, role=Role.CO_SUPERVISOR)
    assert holders.count() == 2
