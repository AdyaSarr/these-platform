"""Team services: CSI incompatibility, removal, role change, CSI period and extension."""

import datetime as dt
from typing import Any

import pytest
import time_machine
from django.core.exceptions import PermissionDenied
from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.accounts import services
from apps.accounts.models import Membership
from apps.accounts.roles import Role
from apps.accounts.services import TeamError
from apps.accounts.tests.factories import (
    MembershipFactory,
    OrganizationFactory,
    ThesisFactory,
    UserFactory,
)

pytestmark = pytest.mark.django_db

NOW = dt.datetime(2026, 12, 1, 10, 0, tzinfo=dt.UTC)
SUPERVISION = [Role.PHD_STUDENT, Role.DIRECTOR, Role.CO_SUPERVISOR, Role.INDUSTRIAL_TUTOR]


@pytest.fixture
def student() -> Membership:
    return MembershipFactory.create(role=Role.PHD_STUDENT)


def _member(student: Membership, role: Role, **kwargs: object) -> Membership:
    return MembershipFactory.create(thesis=student.thesis, role=role, **kwargs)


# --------------------------------------------------------------------------- CSI incompatibility


@pytest.mark.parametrize("other", SUPERVISION)
def test_database_refuses_csi_overlapping_supervision_role(other: Role) -> None:
    m = MembershipFactory.create(role=other)
    with pytest.raises(IntegrityError), transaction.atomic():
        MembershipFactory.create(user=m.user, thesis=m.thesis, role=Role.CSI_MEMBER)


@pytest.mark.parametrize("other", SUPERVISION)
def test_database_refuses_supervision_role_overlapping_csi(other: Role) -> None:
    m = MembershipFactory.create(role=Role.CSI_MEMBER)
    with pytest.raises(IntegrityError), transaction.atomic():
        MembershipFactory.create(user=m.user, thesis=m.thesis, role=other)


def test_csi_on_another_thesis_is_fine() -> None:
    m = MembershipFactory.create(role=Role.DIRECTOR)
    MembershipFactory.create(user=m.user, role=Role.CSI_MEMBER)


def test_guest_and_csi_can_be_combined() -> None:
    m = MembershipFactory.create(role=Role.CSI_MEMBER)
    MembershipFactory.create(user=m.user, thesis=m.thesis, role=Role.GUEST)


def test_two_supervision_roles_can_be_combined() -> None:
    m = MembershipFactory.create(role=Role.DIRECTOR)
    MembershipFactory.create(user=m.user, thesis=m.thesis, role=Role.INDUSTRIAL_TUTOR)


def test_former_director_can_become_csi_after_leaving() -> None:
    with time_machine.travel(NOW, tick=False):
        m = MembershipFactory.create(role=Role.DIRECTOR, active_from=timezone.now())
    with time_machine.travel(NOW + dt.timedelta(days=1), tick=False):
        Membership.objects.filter(pk=m.pk).update(active_until=timezone.now())
        MembershipFactory.create(
            user=m.user, thesis=m.thesis, role=Role.CSI_MEMBER, active_from=timezone.now()
        )


def test_add_membership_reports_incompatibility_as_team_error(student: Membership) -> None:
    director = _member(student, Role.DIRECTOR)
    with pytest.raises(TeamError):
        services.add_membership(
            user=director.user,
            thesis=student.thesis,
            role=Role.CSI_MEMBER,
            organization=director.organization,
        )


# --------------------------------------------------------------------------- add_membership


@time_machine.travel(NOW, tick=False)
def test_csi_membership_ends_after_default_period(student: Membership) -> None:
    m = services.add_membership(
        user=UserFactory.create(),
        thesis=student.thesis,
        role=Role.CSI_MEMBER,
        organization=OrganizationFactory.create(),
    )
    assert m.active_from == NOW
    assert m.active_until == NOW + dt.timedelta(days=60)


def test_csi_membership_expires_by_itself(student: Membership) -> None:
    """Acceptance (CSI addition): no action needed for the CSI access to end."""
    with time_machine.travel(NOW, tick=False):
        m = services.add_membership(
            user=UserFactory.create(),
            thesis=student.thesis,
            role=Role.CSI_MEMBER,
            organization=OrganizationFactory.create(),
        )
    with time_machine.travel(NOW + dt.timedelta(days=60) - dt.timedelta(seconds=1)):
        assert Membership.objects.active().filter(pk=m.pk).exists()
    with time_machine.travel(NOW + dt.timedelta(days=60)):
        assert not Membership.objects.active().filter(pk=m.pk).exists()


@pytest.mark.parametrize("role", [*SUPERVISION[1:], Role.GUEST])
def test_other_memberships_are_open_ended(student: Membership, role: Role) -> None:
    m = services.add_membership(
        user=UserFactory.create(),
        thesis=student.thesis,
        role=role,
        organization=OrganizationFactory.create(),
    )
    assert m.active_until is None


# --------------------------------------------------------------------------- end_membership


@time_machine.travel(NOW, tick=False)
def test_student_removes_member(student: Membership) -> None:
    director = _member(student, Role.DIRECTOR, active_from=NOW - dt.timedelta(days=30))
    services.end_membership(director, by=student.user)
    director.refresh_from_db()
    assert director.active_until == NOW
    assert Membership.objects.filter(pk=director.pk).exists()  # history kept


@pytest.mark.parametrize("role", [r for r in Role if r is not Role.PHD_STUDENT])
def test_only_student_can_remove(student: Membership, role: Role) -> None:
    actor = _member(student, role)
    target = _member(student, Role.CO_SUPERVISOR)
    with pytest.raises(PermissionDenied):
        services.end_membership(target, by=actor.user)


def test_student_of_another_thesis_cannot_remove(student: Membership) -> None:
    target = _member(student, Role.DIRECTOR)
    other_student = MembershipFactory.create(role=Role.PHD_STUDENT)
    with pytest.raises(PermissionDenied):
        services.end_membership(target, by=other_student.user)


def test_phd_student_membership_cannot_be_ended(student: Membership) -> None:
    with pytest.raises(TeamError):
        services.end_membership(student, by=student.user)


def test_ended_membership_cannot_be_ended_again(student: Membership) -> None:
    with time_machine.travel(NOW, tick=False):
        target = _member(student, Role.DIRECTOR, active_from=NOW - dt.timedelta(days=1))
        services.end_membership(target, by=student.user)
    with time_machine.travel(NOW + dt.timedelta(days=1), tick=False), pytest.raises(TeamError):
        services.end_membership(target, by=student.user)
    target.refresh_from_db()
    assert target.active_until == NOW


@time_machine.travel(NOW, tick=False)
def test_removing_csi_before_expiry_ends_it_now(student: Membership) -> None:
    csi = _member(
        student,
        Role.CSI_MEMBER,
        active_from=NOW - dt.timedelta(days=1),
        active_until=NOW + dt.timedelta(days=59),
    )
    services.end_membership(csi, by=student.user)
    csi.refresh_from_db()
    assert csi.active_until == NOW


# --------------------------------------------------------------------------- change_role


@time_machine.travel(NOW, tick=False)
def test_change_role_closes_old_row_and_opens_new_one(student: Membership) -> None:
    old = _member(student, Role.CO_SUPERVISOR, active_from=NOW - dt.timedelta(days=30))
    new = services.change_role(old, Role.DIRECTOR, by=student.user)
    old.refresh_from_db()
    assert old.active_until == NOW
    assert (new.user, new.thesis, new.role, new.active_from) == (
        old.user,
        old.thesis,
        Role.DIRECTOR,
        NOW,
    )
    assert new.organization == old.organization
    assert new.active_until is None


@pytest.mark.parametrize("role", [r for r in Role if r is not Role.PHD_STUDENT])
def test_only_student_can_change_roles(student: Membership, role: Role) -> None:
    actor = _member(student, role)
    target = _member(student, Role.CO_SUPERVISOR)
    with pytest.raises(PermissionDenied):
        services.change_role(target, Role.DIRECTOR, by=actor.user)


def test_nobody_can_be_promoted_to_phd_student(student: Membership) -> None:
    target = _member(student, Role.DIRECTOR)
    with pytest.raises(TeamError):
        services.change_role(target, Role.PHD_STUDENT, by=student.user)


def test_phd_student_role_cannot_be_changed(student: Membership) -> None:
    with pytest.raises(TeamError):
        services.change_role(student, Role.DIRECTOR, by=student.user)


def test_change_to_same_role_is_refused(student: Membership) -> None:
    target = _member(student, Role.DIRECTOR)
    with pytest.raises(TeamError):
        services.change_role(target, Role.DIRECTOR, by=student.user)


def test_change_role_of_ended_membership_is_refused(student: Membership) -> None:
    target = _member(
        student,
        Role.DIRECTOR,
        active_from=timezone.now() - dt.timedelta(days=2),
        active_until=timezone.now() - dt.timedelta(days=1),
    )
    with pytest.raises(TeamError):
        services.change_role(target, Role.CO_SUPERVISOR, by=student.user)


def test_change_role_into_csi_conflicting_with_other_role_fails_atomically(
    student: Membership,
) -> None:
    director = _member(student, Role.DIRECTOR)
    tutor = MembershipFactory.create(
        user=director.user, thesis=student.thesis, role=Role.INDUSTRIAL_TUTOR
    )
    with pytest.raises(TeamError):
        services.change_role(director, Role.CSI_MEMBER, by=student.user)
    director.refresh_from_db()
    assert director.active_until is None  # nothing was closed
    assert tutor.pk in Membership.objects.active().values_list("pk", flat=True)


@time_machine.travel(NOW, tick=False)
def test_change_role_into_csi_gets_default_period(student: Membership) -> None:
    guest = _member(student, Role.GUEST, active_from=NOW - dt.timedelta(days=1))
    new = services.change_role(guest, Role.CSI_MEMBER, by=student.user)
    assert new.active_until == NOW + dt.timedelta(days=60)


# --------------------------------------------------------------------------- extend_membership


@time_machine.travel(NOW, tick=False)
def test_student_extends_csi(student: Membership) -> None:
    csi = _member(
        student,
        Role.CSI_MEMBER,
        active_from=NOW - dt.timedelta(days=50),
        active_until=NOW + dt.timedelta(days=10),
    )
    services.extend_membership(csi, days=60, by=student.user)
    csi.refresh_from_db()
    assert csi.active_until == NOW + dt.timedelta(days=70)


@time_machine.travel(NOW, tick=False)
@pytest.mark.parametrize(("days", "expected"), [(None, 60), (1, 1), (90, 90)])
def test_extension_default_and_bounds_accepted(
    student: Membership, days: int | None, expected: int
) -> None:
    end = NOW + dt.timedelta(days=5)
    csi = _member(
        student, Role.CSI_MEMBER, active_from=NOW - dt.timedelta(days=1), active_until=end
    )
    kwargs: dict[str, Any] = {} if days is None else {"days": days}
    services.extend_membership(csi, by=student.user, **kwargs)
    csi.refresh_from_db()
    assert csi.active_until == end + dt.timedelta(days=expected)


@pytest.mark.parametrize("role", [r for r in Role if r is not Role.PHD_STUDENT])
def test_only_student_can_extend(student: Membership, role: Role) -> None:
    actor = _member(student, role) if role is not Role.CSI_MEMBER else None
    csi = _member(student, Role.CSI_MEMBER, active_until=timezone.now() + dt.timedelta(days=5))
    by = actor.user if actor else csi.user  # a CSI member trying to extend themself
    with pytest.raises(PermissionDenied):
        services.extend_membership(csi, days=30, by=by)


def test_only_csi_memberships_can_be_extended(student: Membership) -> None:
    director = _member(student, Role.DIRECTOR)
    with pytest.raises(TeamError):
        services.extend_membership(director, days=30, by=student.user)


def test_expired_csi_cannot_be_extended(student: Membership) -> None:
    """Re-admitting an expired CSI member goes through a new invitation."""
    csi = _member(
        student,
        Role.CSI_MEMBER,
        active_from=timezone.now() - dt.timedelta(days=61),
        active_until=timezone.now() - dt.timedelta(days=1),
    )
    with pytest.raises(TeamError):
        services.extend_membership(csi, days=30, by=student.user)


@pytest.mark.parametrize("days", [0, -1, 91, 180])
def test_extension_is_bounded(student: Membership, days: int) -> None:
    csi = _member(student, Role.CSI_MEMBER, active_until=timezone.now() + dt.timedelta(days=5))
    with pytest.raises(TeamError):
        services.extend_membership(csi, days=days, by=student.user)


def test_services_refuse_membership_of_another_thesis_owner(student: Membership) -> None:
    """Defence in depth: rights are checked on the target's thesis, not the actor's."""
    other = MembershipFactory.create(thesis=ThesisFactory.create(), role=Role.DIRECTOR)
    with pytest.raises(PermissionDenied):
        services.change_role(other, Role.CO_SUPERVISOR, by=student.user)
