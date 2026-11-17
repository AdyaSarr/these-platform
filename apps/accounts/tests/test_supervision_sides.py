"""The company co-supervisor role, and one side of the supervision per person.

- INDUSTRIAL_CO_SUPERVISOR (« Co-encadrant·e entreprise ») joins the roles;
  CO_SUPERVISOR is now labelled « Co-encadrant·e LIRMM » (same stored value).
- The supervision has two sides: laboratory {DIRECTOR, CO_SUPERVISOR} and company
  {INDUSTRIAL_TUTOR, INDUSTRIAL_CO_SUPERVISOR}. One person may not hold roles of
  both sides on one thesis over overlapping periods (database constraint
  accounts_membership_one_supervision_side); roles of one side may be cumulated.
  The team services refuse it early, with a clear message: at the invitation, at
  the acceptance and at a role change.
- Several holders of one role on a thesis stay allowed (two tutors, two
  co-supervisors).
- Every column that stores a role takes 32 characters (the new value has 24).
"""

from __future__ import annotations

import datetime as dt

import pytest
import time_machine
from django.db import IntegrityError, transaction
from django.utils import timezone, translation

from apps.accounts import services
from apps.accounts.models import Invitation, Membership
from apps.accounts.roles import CSI_INCOMPATIBLE_ROLES, Role
from apps.accounts.services import TeamError
from apps.accounts.tests.factories import MembershipFactory, OrganizationFactory, UserFactory

pytestmark = pytest.mark.django_db

R = Role
ICS = Role.INDUSTRIAL_CO_SUPERVISOR
LAB = (R.DIRECTOR, R.CO_SUPERVISOR)
COMPANY = (R.INDUSTRIAL_TUTOR, ICS)
CROSS = [(lab, company) for lab in LAB for company in COMPANY]
CROSS_BOTH_ORDERS = CROSS + [(b, a) for a, b in CROSS]
NOW = dt.datetime(2026, 12, 1, 10, 0, tzinfo=dt.UTC)
SIDE_MESSAGE = "côté laboratoire et côté entreprise"


def _ids(pair: tuple[Role, Role]) -> str:
    return f"{pair[0].value}+{pair[1].value}"


# --------------------------------------------------------------------------- the role


def test_the_new_role_and_the_labels() -> None:
    assert ICS.value == "INDUSTRIAL_CO_SUPERVISOR"
    assert R.CO_SUPERVISOR.value == "CO_SUPERVISOR"  # the stored value does not change
    order = list(Role)
    assert order.index(ICS) == order.index(R.INDUSTRIAL_TUTOR) + 1
    with translation.override("fr"):
        assert str(R.CO_SUPERVISOR.label) == "Co-encadrant·e LIRMM"
        assert str(ICS.label) == "Co-encadrant·e entreprise"


def test_the_company_co_supervisor_may_not_sit_on_the_csi() -> None:
    assert ICS in CSI_INCOMPATIBLE_ROLES


@pytest.mark.parametrize(
    ("app", "model", "field"),
    [
        ("accounts", "Membership", "role"),
        ("accounts", "Invitation", "role"),
        ("admin_file", "ItemApproval", "role"),
        ("admin_file", "RequiredItem", "validator_roles"),
    ],
)
def test_every_role_column_takes_32_characters(app: str, model: str, field: str) -> None:
    from django.apps import apps

    column = apps.get_model(app, model)._meta.get_field(field)
    column = getattr(column, "base_field", column)  # the ArrayField's elements
    assert column.max_length == 32
    assert ICS.value in {value for value, _ in column.choices}


def test_a_company_co_supervisor_membership_is_stored() -> None:
    m = MembershipFactory.create(role=ICS)
    assert Membership.objects.get(pk=m.pk).role == ICS.value


# --------------------------------------------------------------------------- database


@pytest.mark.parametrize("pair", CROSS_BOTH_ORDERS, ids=_ids)
def test_the_database_refuses_both_sides_at_once(pair: tuple[Role, Role]) -> None:
    first, second = pair
    m = MembershipFactory.create(role=first)
    with pytest.raises(IntegrityError, match="one_supervision_side"), transaction.atomic():
        MembershipFactory.create(user=m.user, thesis=m.thesis, role=second)


@pytest.mark.parametrize("pair", [LAB, COMPANY], ids=_ids)
def test_two_roles_of_one_side_may_be_cumulated(pair: tuple[Role, Role]) -> None:
    m = MembershipFactory.create(role=pair[0])
    MembershipFactory.create(user=m.user, thesis=m.thesis, role=pair[1])
    assert Membership.objects.active().filter(user=m.user, thesis=m.thesis).count() == 2


@pytest.mark.parametrize("pair", CROSS_BOTH_ORDERS, ids=_ids)
def test_one_side_after_the_other_is_accepted(pair: tuple[Role, Role]) -> None:
    """A role change closes [a, t) and opens [t, ...): the periods do not overlap."""
    first, second = pair
    with time_machine.travel(NOW, tick=False):
        m = MembershipFactory.create(role=first, active_from=NOW - dt.timedelta(days=30))
        Membership.objects.filter(pk=m.pk).update(active_until=NOW)
        MembershipFactory.create(user=m.user, thesis=m.thesis, role=second, active_from=NOW)


@pytest.mark.parametrize("pair", CROSS_BOTH_ORDERS, ids=_ids)
def test_both_sides_on_two_different_theses_are_accepted(pair: tuple[Role, Role]) -> None:
    m = MembershipFactory.create(role=pair[0])
    other = MembershipFactory.create(user=m.user, role=pair[1])
    assert other.thesis_id != m.thesis_id


def test_a_later_row_of_the_other_side_still_conflicts() -> None:
    """A row scheduled to end later (still active) blocks the other side."""
    m = MembershipFactory.create(
        role=R.DIRECTOR, active_until=timezone.now() + dt.timedelta(days=60)
    )
    with pytest.raises(IntegrityError), transaction.atomic():
        MembershipFactory.create(user=m.user, thesis=m.thesis, role=ICS)


@pytest.mark.parametrize("order", ["csi_first", "ics_first"])
def test_the_database_keeps_the_csi_apart_from_the_company_co_supervisor(order: str) -> None:
    roles = (R.CSI_MEMBER, ICS) if order == "csi_first" else (ICS, R.CSI_MEMBER)
    m = MembershipFactory.create(role=roles[0])
    with pytest.raises(IntegrityError, match="csi_independent"), transaction.atomic():
        MembershipFactory.create(user=m.user, thesis=m.thesis, role=roles[1])


# --------------------------------------------------------------------------- several holders


@pytest.mark.parametrize("role", [R.INDUSTRIAL_TUTOR, R.CO_SUPERVISOR, ICS, R.DIRECTOR])
def test_several_people_may_hold_one_role(role: Role) -> None:
    first = MembershipFactory.create(role=role)
    second = MembershipFactory.create(thesis=first.thesis, role=role)
    held = Membership.objects.active().filter(thesis=first.thesis, role=role)
    assert {m.user_id for m in held} == {first.user_id, second.user_id}


# --------------------------------------------------------------------------- services


@pytest.fixture
def student() -> Membership:
    return MembershipFactory.create(role=R.PHD_STUDENT)


def _invite(student: Membership, email: str, role: Role) -> Invitation:
    return services.create_invitation(
        thesis=student.thesis,
        email=email,
        role=role,
        organization=OrganizationFactory.create(),
        by=student.user,
    )


@pytest.mark.parametrize("pair", CROSS_BOTH_ORDERS, ids=_ids)
def test_the_invitation_is_refused_early_and_clearly(
    student: Membership, pair: tuple[Role, Role]
) -> None:
    held, invited = pair
    person = MembershipFactory.create(thesis=student.thesis, role=held).user
    with pytest.raises(TeamError) as refused:
        _invite(student, person.email, invited)
    assert SIDE_MESSAGE in str(refused.value)
    assert not Invitation.objects.filter(email=person.email, thesis=student.thesis).exists()


@pytest.mark.parametrize("pair", [LAB, COMPANY], ids=_ids)
def test_an_invitation_on_the_same_side_is_accepted(
    student: Membership, pair: tuple[Role, Role]
) -> None:
    person = MembershipFactory.create(thesis=student.thesis, role=pair[0]).user
    _invite(student, person.email, pair[1])


@pytest.mark.parametrize("pair", CROSS_BOTH_ORDERS, ids=_ids)
def test_the_acceptance_is_refused_clearly_and_keeps_the_invitation(
    student: Membership, pair: tuple[Role, Role]
) -> None:
    """The other side was given after the invitation was sent."""
    held, invited = pair
    person = UserFactory.create(email="plus-tard@example.org")
    inv = _invite(student, person.email, invited)
    MembershipFactory.create(user=person, thesis=student.thesis, role=held)
    with pytest.raises(TeamError) as refused:
        services.accept_invitation(inv.pk, user=person)
    assert SIDE_MESSAGE in str(refused.value)
    inv.refresh_from_db()
    assert inv.used_at is None
    assert not Membership.objects.filter(user=person, role=invited).exists()


@pytest.mark.parametrize("pair", CROSS_BOTH_ORDERS, ids=_ids)
def test_a_role_change_to_the_other_side_alone_is_accepted(
    student: Membership, pair: tuple[Role, Role]
) -> None:
    old, new = pair
    m = MembershipFactory.create(thesis=student.thesis, role=old)
    changed = services.change_role(m, new, by=student.user)
    assert changed.role == new.value
    held = Membership.objects.active().filter(user=m.user, thesis=student.thesis)
    assert {x.role for x in held} == {new.value}


def test_a_role_change_that_would_mix_the_sides_is_refused_and_closes_nothing(
    student: Membership,
) -> None:
    """Director and LIRMM co-supervisor; the co-supervision moves to the company
    side while the direction stays: refused, before anything is closed."""
    director = MembershipFactory.create(thesis=student.thesis, role=R.DIRECTOR)
    co_sup = MembershipFactory.create(
        user=director.user, thesis=student.thesis, role=R.CO_SUPERVISOR
    )
    for new in COMPANY:
        with pytest.raises(TeamError) as refused:
            services.change_role(co_sup, new, by=student.user)
        assert SIDE_MESSAGE in str(refused.value)
        co_sup.refresh_from_db()
        assert co_sup.active_until is None
    held = Membership.objects.active().filter(user=director.user, thesis=student.thesis)
    assert {x.role for x in held} == {R.DIRECTOR.value, R.CO_SUPERVISOR.value}


def test_the_message_names_no_person(student: Membership) -> None:
    person = MembershipFactory.create(
        thesis=student.thesis, role=R.DIRECTOR, user__email="temoin.fuite@example.org"
    ).user
    with pytest.raises(TeamError) as refused:
        _invite(student, person.email, ICS)
    assert "temoin.fuite" not in str(refused.value)


# --------------------------------------------------------------------------- the student alone
# Same family: the PhD student holds no other role on their own thesis, periods
# that overlap (database constraint accounts_membership_student_alone), or they
# could validate their own items and objectives. Refused early by the services.

STUDENT_ALONE = "aucun autre rôle"
OTHER_ROLES = [r for r in Role if r is not R.PHD_STUDENT]


@pytest.mark.parametrize("role", OTHER_ROLES, ids=lambda r: r.value)
def test_the_database_refuses_another_role_to_the_student(role: Role) -> None:
    m = MembershipFactory.create(role=R.PHD_STUDENT)
    with pytest.raises(IntegrityError), transaction.atomic():
        MembershipFactory.create(user=m.user, thesis=m.thesis, role=role)


@pytest.mark.parametrize("role", [r for r in OTHER_ROLES if r is not R.CSI_MEMBER])
def test_the_student_constraint_is_the_one_that_refuses(role: Role) -> None:
    m = MembershipFactory.create(role=R.PHD_STUDENT)
    with pytest.raises(IntegrityError, match="student_alone"), transaction.atomic():
        MembershipFactory.create(user=m.user, thesis=m.thesis, role=role)


@pytest.mark.parametrize("role", OTHER_ROLES, ids=lambda r: r.value)
def test_the_database_refuses_the_student_role_to_a_member(role: Role) -> None:
    m = MembershipFactory.create(role=role)
    with pytest.raises(IntegrityError), transaction.atomic():
        MembershipFactory.create(user=m.user, thesis=m.thesis, role=R.PHD_STUDENT)


@pytest.mark.parametrize("role", OTHER_ROLES, ids=lambda r: r.value)
def test_after_the_student_period_another_role_is_accepted(role: Role) -> None:
    with time_machine.travel(NOW, tick=False):
        m = MembershipFactory.create(role=R.PHD_STUDENT, active_from=NOW - dt.timedelta(days=30))
        Membership.objects.filter(pk=m.pk).update(active_until=NOW)
        MembershipFactory.create(user=m.user, thesis=m.thesis, role=role, active_from=NOW)


@pytest.mark.parametrize("role", OTHER_ROLES, ids=lambda r: r.value)
def test_another_role_on_another_thesis_is_accepted(role: Role) -> None:
    m = MembershipFactory.create(role=R.PHD_STUDENT)
    other = MembershipFactory.create(user=m.user, role=role)
    assert other.thesis_id != m.thesis_id


@pytest.mark.parametrize(
    "role", [r for r in OTHER_ROLES if r is not R.CSI_MEMBER], ids=lambda r: r.value
)
def test_inviting_the_student_to_another_role_is_refused_early(
    student: Membership, role: Role
) -> None:
    with pytest.raises(TeamError) as refused:
        _invite(student, student.user.email, role)
    assert STUDENT_ALONE in str(refused.value)
    assert not Invitation.objects.filter(thesis=student.thesis, role=role).exists()


def test_inviting_the_student_to_the_csi_keeps_the_csi_message(student: Membership) -> None:
    with pytest.raises(TeamError) as refused:
        _invite(student, student.user.email, R.CSI_MEMBER)
    assert "CSI" in str(refused.value)


def test_the_acceptance_by_the_student_is_refused_clearly(student: Membership) -> None:
    """The invitation went to an address the student takes afterwards."""
    inv = _invite(student, "nouvelle-adresse@example.org", R.DIRECTOR)
    student.user.email = "nouvelle-adresse@example.org"
    student.user.save(update_fields=["email"])
    with pytest.raises(TeamError) as refused:
        services.accept_invitation(inv.pk, user=student.user)
    assert STUDENT_ALONE in str(refused.value)
    inv.refresh_from_db()
    assert inv.used_at is None
    held = Membership.objects.active().filter(user=student.user, thesis=student.thesis)
    assert {m.role for m in held} == {R.PHD_STUDENT.value}


@pytest.mark.parametrize("role", OTHER_ROLES, ids=lambda r: r.value)
def test_the_student_role_is_never_changed(student: Membership, role: Role) -> None:
    with pytest.raises(TeamError):
        services.change_role(student, role, by=student.user)
    student.refresh_from_db()
    assert student.active_until is None
