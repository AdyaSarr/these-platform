"""Per-item authorization (validator_roles), separation of duties, audited refusals.

Called at SERVICE level, without views: the thesis guard is not what is tested.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from django.core.exceptions import PermissionDenied, ValidationError

from apps.accounts.models import Thesis
from apps.accounts.roles import Role
from apps.accounts.tests.factories import MembershipFactory
from apps.admin_file import services
from apps.admin_file.models import ItemApproval, ItemStatus, Reason, RequiredItem, Section
from apps.admin_file.tests.helpers import item_of, make_team, put
from apps.admin_file.transitions import FrozenFileError
from apps.vault.models import AuditEvent

pytestmark = pytest.mark.django_db

S = ItemStatus
D, T = Role.DIRECTOR, Role.INDUSTRIAL_TUTOR
ROLES = ("student", "director", "co_supervisor", "tutor", "csi", "guest")


@pytest.fixture
def team() -> SimpleNamespace:
    return make_team()


def _denials(user: Any) -> list[AuditEvent]:
    return list(AuditEvent.objects.filter(action="ADMIN_ITEM_ACCESS_DENIED", actor=user))


def _submitted(team: SimpleNamespace, **kwargs: Any) -> RequiredItem:
    item = item_of(team, **kwargs)
    services.submit(item=item, user=team.student)
    item.refresh_from_db()
    return item


def _refused(call: Any, user: Any, item: RequiredItem, reason: str) -> None:
    before = (RequiredItem.objects.get(pk=item.pk).status, ItemApproval.objects.count())
    with pytest.raises(PermissionDenied):
        call()
    assert (RequiredItem.objects.get(pk=item.pk).status, ItemApproval.objects.count()) == before
    [event] = _denials(user)[-1:]
    assert event.object_id == str(item.pk)
    assert event.details_json["reason"] == reason


# --------------------------------------------------------------------------- who validates


@pytest.mark.parametrize(
    ("section", "validators", "allowed"),
    [
        (Section.CIFRE, [T], {"tutor"}),  # e.g. employment contract
        (Section.CIFRE, [D], {"director"}),  # e.g. proof of funding
        (Section.ACADEMIC, [D], {"director"}),  # e.g. ADUM
    ],
)
def test_only_the_item_validator_roles_approve(
    team: SimpleNamespace, section: Section, validators: list[Role], allowed: set[str]
) -> None:
    for role in ROLES:
        item = _submitted(team, section=section, validator_roles=validators)
        user = getattr(team, role)
        if role in allowed:
            assert services.approve(item=item, user=user).status == S.VALIDATED
        else:
            with pytest.raises(PermissionDenied):
                services.approve(item=item, user=user)
            assert RequiredItem.objects.get(pk=item.pk).status == S.SUBMITTED


def test_director_cannot_validate_the_tutors_item_although_they_read_it(
    team: SimpleNamespace,
) -> None:
    """The per-item rule is validator_roles, NOT the section's read right."""
    item = _submitted(team, code="employment_contract", section=Section.CIFRE, validator_roles=[T])
    for call in (
        lambda: services.approve(item=item, user=team.director),
        lambda: services.reject(item=item, user=team.director, reason=Reason.ILLEGIBLE),
    ):
        _refused(call, team.director, item, "not_validator")


def test_co_supervisor_cannot_validate(team: SimpleNamespace) -> None:
    """Acceptance (M03 fiche), at service level: the guard is not involved."""
    item = _submitted(team, section=Section.ACADEMIC, validator_roles=[D])
    _refused(
        lambda: services.approve(item=item, user=team.co_supervisor),
        team.co_supervisor,
        item,
        "not_visible",
    )


def test_tutor_cannot_touch_an_academic_item(team: SimpleNamespace) -> None:
    item = _submitted(team, section=Section.ACADEMIC, validator_roles=[D])
    _refused(lambda: services.approve(item=item, user=team.tutor), team.tutor, item, "not_visible")


@pytest.mark.parametrize("role", ["director", "co_supervisor", "tutor", "csi", "guest"])
def test_only_the_student_submits_and_withdraws(team: SimpleNamespace, role: str) -> None:
    user = getattr(team, role)
    item = item_of(team, section=Section.CIFRE, validator_roles=[D])
    with pytest.raises(PermissionDenied):
        services.submit(item=item, user=user)
    services.submit(item=item, user=team.student)
    with pytest.raises(PermissionDenied):
        services.withdraw(item=item, user=user)
    assert RequiredItem.objects.get(pk=item.pk).status == S.SUBMITTED


def test_reopen_by_student_or_validator_only(team: SimpleNamespace) -> None:
    item = item_of(team, section=Section.CIFRE, validator_roles=[T])
    for role in ("director", "co_supervisor", "csi", "guest"):
        put(item, S.VALIDATED, team.student)
        with pytest.raises(PermissionDenied):
            services.reopen(item=item, user=getattr(team, role), reason=Reason.AMENDMENT)
    for role in ("student", "tutor"):
        put(item, S.VALIDATED, team.student)
        assert services.reopen(item=item, user=getattr(team, role), reason=Reason.AMENDMENT)


# --------------------------------------------------------------------------- two parties


def test_two_party_item_needs_both_roles(team: SimpleNamespace) -> None:
    item = _submitted(team, section=Section.CIFRE, validator_roles=[D, T])
    assert services.approve(item=item, user=team.director).status == S.SUBMITTED
    assert services.approve(item=item, user=team.tutor).status == S.VALIDATED
    roles = set(ItemApproval.objects.filter(item=item).values_list("role", flat=True))
    assert roles == {D, T}


def test_a_role_approves_a_cycle_once(team: SimpleNamespace) -> None:
    item = _submitted(team, section=Section.CIFRE, validator_roles=[D, T])
    services.approve(item=item, user=team.director)
    other_director = MembershipFactory.create(thesis=team.thesis, role=D, user__mfa=False).user
    _refused(
        lambda: services.approve(item=item, user=other_director),
        other_director,
        item,
        "already_approved",
    )


def test_one_person_with_both_roles_cannot_validate_the_collaboration_contract_alone(
    team: SimpleNamespace,
) -> None:
    """Correction 1: one approval per PERSON and cycle, roles cumulated or not."""
    both = MembershipFactory.create(thesis=team.thesis, role=D, user__mfa=False).user
    MembershipFactory.create(thesis=team.thesis, role=T, user=both)
    item = _submitted(
        team, code="collaboration_contract", section=Section.CIFRE, validator_roles=[D, T]
    )
    assert services.approve(item=item, user=both).status == S.SUBMITTED
    _refused(lambda: services.approve(item=item, user=both), both, item, "already_approved")
    assert ItemApproval.objects.filter(item=item).count() == 1
    # The other party still validates.
    assert services.approve(item=item, user=team.tutor).status == S.VALIDATED


def test_the_submitter_never_validates_their_cycle(team: SimpleNamespace) -> None:
    """Correction 1: a student who is ALSO director of the thesis."""
    MembershipFactory.create(thesis=team.thesis, role=D, user=team.student)
    item = _submitted(team, section=Section.ACADEMIC, validator_roles=[D])
    for call in (
        lambda: services.approve(item=item, user=team.student),
        lambda: services.reject(item=item, user=team.student, reason=Reason.OTHER),
    ):
        _refused(call, team.student, item, "submitter")
    assert services.approve(item=item, user=team.director).status == S.VALIDATED


def test_a_new_cycle_needs_new_approvals(team: SimpleNamespace) -> None:
    item = _submitted(team, section=Section.CIFRE, validator_roles=[D, T])
    services.approve(item=item, user=team.director)
    services.reject(item=item, user=team.tutor, reason=Reason.MISSING_SIGNATURE)
    services.submit(item=item, user=team.student)
    assert services.approve(item=item, user=team.tutor).status == S.SUBMITTED
    assert services.approve(item=item, user=team.director).status == S.VALIDATED


def test_the_approval_records_cycle_submitter_and_version(team: SimpleNamespace) -> None:
    item = _submitted(team, validator_roles=[D])
    services.approve(item=item, user=team.director)
    approval = ItemApproval.objects.get(item=item)
    assert (approval.cycle, approval.role, approval.user, approval.submitted_by) == (
        1,
        D,
        team.director,
        team.student,
    )
    assert approval.created_by == team.director


# --------------------------------------------------------------------------- reasons


def test_reject_and_reopen_need_a_known_reason(team: SimpleNamespace) -> None:
    item = _submitted(team)
    for bad in ("", "WHATEVER", None):
        with pytest.raises(ValidationError):
            services.reject(item=item, user=team.director, reason=bad)
    assert RequiredItem.objects.get(pk=item.pk).status == S.SUBMITTED


@pytest.mark.parametrize("comment", ["x" * 281, "ligne\x00nulle", "saut\u0007"])
def test_the_comment_is_bounded_and_clean(team: SimpleNamespace, comment: str) -> None:
    item = _submitted(team)
    with pytest.raises(ValidationError):
        services.reject(item=item, user=team.director, reason=Reason.OTHER, comment=comment)


def test_the_comment_never_reaches_the_audit_log(team: SimpleNamespace) -> None:
    item = _submitted(team)
    services.reject(
        item=item, user=team.director, reason=Reason.OTHER, comment="Commentaire fictif"
    )
    event = AuditEvent.objects.get(action="ADMIN_ITEM_REJECTED")
    assert event.details_json["reason"] == "OTHER"
    assert "Commentaire" not in str(event.details_json)


# --------------------------------------------------------------------------- frozen, IDOR


@pytest.mark.parametrize("status", [Thesis.Status.DEFENDED, Thesis.Status.CLOSED])
def test_a_finished_thesis_freezes_its_file(status: str) -> None:
    team = make_team(status=status)
    item = item_of(team)
    with pytest.raises(FrozenFileError):
        services.submit(item=item, user=team.student)
    put(item, S.SUBMITTED, team.student)
    with pytest.raises(FrozenFileError):
        services.approve(item=item, user=team.director)


def test_visible_items_follow_the_sections(team: SimpleNamespace) -> None:
    cifre = item_of(team, section=Section.CIFRE, validator_roles=[T])
    academic = item_of(team, section=Section.ACADEMIC, validator_roles=[D])
    expected = {
        "student": {cifre, academic},
        "director": {cifre, academic},
        "tutor": {cifre},
        "co_supervisor": set(),
        "csi": set(),
        "guest": set(),
    }
    for role, items in expected.items():
        assert set(services.visible_items(getattr(team, role), team.thesis)) == items, role


def test_item_for_refuses_another_thesis_and_audits(team: SimpleNamespace) -> None:
    foreign = item_of(make_team())
    with pytest.raises(PermissionDenied):
        services.item_for(team.director, team.thesis, foreign.pk, operation="detail")
    with pytest.raises(PermissionDenied):
        services.item_for(team.director, team.thesis, "not-a-uuid", operation="detail")
    events = _denials(team.director)
    assert len(events) == 2
    assert all(e.thesis_id == team.thesis.pk for e in events)


def test_item_for_refuses_an_invisible_item(team: SimpleNamespace) -> None:
    academic = item_of(team, section=Section.ACADEMIC)
    with pytest.raises(PermissionDenied):
        services.item_for(team.tutor, team.thesis, academic.pk, operation="detail")
    assert services.item_for(team.director, team.thesis, academic.pk, operation="detail") == (
        academic
    )
