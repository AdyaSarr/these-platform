"""Organization, Thesis, Membership: database constraints and the ``active`` notion.

Constraints are enforced by PostgreSQL itself (not only by Python), so these
tests write rows directly and expect IntegrityError.
"""

import datetime as dt
import uuid

import pytest
import time_machine
from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.accounts.models import Membership, Thesis
from apps.accounts.roles import Role
from apps.accounts.tests.factories import MembershipFactory, ThesisFactory, UserFactory

pytestmark = pytest.mark.django_db

NOW = dt.datetime(2026, 12, 1, 10, 0, tzinfo=dt.UTC)


def test_primary_keys_are_uuid() -> None:
    m = MembershipFactory.create()
    for obj in (m, m.thesis, m.organization):
        assert isinstance(obj.pk, uuid.UUID)


def test_thesis_defaults_to_setup() -> None:
    thesis = Thesis.objects.create(
        title="Thèse fictive",
        cifre_start_date=dt.date(2026, 3, 1),
        first_enrollment_date=dt.date(2026, 3, 1),
        planned_end_date=dt.date(2029, 3, 1),
    )
    assert thesis.status == Thesis.Status.SETUP


def test_thesis_end_must_follow_start() -> None:
    with pytest.raises(IntegrityError), transaction.atomic():
        ThesisFactory.create(
            cifre_start_date=dt.date(2026, 3, 1), planned_end_date=dt.date(2026, 3, 1)
        )


# --------------------------------------------------------------------------- uniqueness


def test_same_active_role_twice_is_rejected() -> None:
    m = MembershipFactory.create(role=Role.DIRECTOR)
    with pytest.raises(IntegrityError), transaction.atomic():
        MembershipFactory.create(user=m.user, thesis=m.thesis, role=Role.DIRECTOR)


def test_member_can_be_readmitted_after_removal() -> None:
    with time_machine.travel(NOW, tick=False):
        m = MembershipFactory.create(role=Role.DIRECTOR, active_from=timezone.now())
    with time_machine.travel(NOW + dt.timedelta(days=1), tick=False):
        m.active_until = timezone.now()
        m.save()
        again = MembershipFactory.create(
            user=m.user, thesis=m.thesis, role=Role.DIRECTOR, active_from=timezone.now()
        )
    assert again.pk != m.pk


def test_overlapping_periods_for_same_role_are_rejected() -> None:
    """A row scheduled to end later (e.g. CSI + 60 days) still blocks a duplicate."""
    m = MembershipFactory.create(
        role=Role.CSI_MEMBER, active_until=timezone.now() + dt.timedelta(days=60)
    )
    with pytest.raises(IntegrityError), transaction.atomic():
        MembershipFactory.create(user=m.user, thesis=m.thesis, role=Role.CSI_MEMBER)


def test_same_user_can_hold_two_different_roles() -> None:
    m = MembershipFactory.create(role=Role.DIRECTOR)
    MembershipFactory.create(user=m.user, thesis=m.thesis, role=Role.CO_SUPERVISOR)
    assert Membership.objects.filter(user=m.user, thesis=m.thesis).count() == 2


def test_only_one_active_phd_student_per_thesis() -> None:
    m = MembershipFactory.create(role=Role.PHD_STUDENT)
    with pytest.raises(IntegrityError), transaction.atomic():
        MembershipFactory.create(thesis=m.thesis, role=Role.PHD_STUDENT)


def test_new_phd_student_allowed_once_previous_has_ended() -> None:
    with time_machine.travel(NOW, tick=False):
        m = MembershipFactory.create(role=Role.PHD_STUDENT, active_from=timezone.now())
    with time_machine.travel(NOW + dt.timedelta(days=1), tick=False):
        m.active_until = timezone.now()
        m.save()
        MembershipFactory.create(thesis=m.thesis, role=Role.PHD_STUDENT, active_from=timezone.now())


def test_phd_students_of_different_theses_do_not_conflict() -> None:
    MembershipFactory.create(role=Role.PHD_STUDENT)
    MembershipFactory.create(role=Role.PHD_STUDENT)


def test_membership_cannot_end_before_it_starts() -> None:
    with pytest.raises(IntegrityError), transaction.atomic():
        MembershipFactory.create(active_from=NOW, active_until=NOW - dt.timedelta(seconds=1))


# --------------------------------------------------------------------------- active()


@pytest.mark.parametrize(
    ("active_from", "active_until", "expected"),
    [
        (NOW - dt.timedelta(days=1), None, True),
        (NOW - dt.timedelta(days=1), NOW + dt.timedelta(seconds=1), True),
        (NOW - dt.timedelta(days=1), NOW, False),  # ends exactly now: half-open
        (NOW - dt.timedelta(days=2), NOW - dt.timedelta(days=1), False),
        (NOW + dt.timedelta(seconds=1), None, False),  # not started yet
        (NOW, None, True),
    ],
)
def test_active_period_is_half_open(
    active_from: dt.datetime, active_until: dt.datetime | None, expected: bool
) -> None:
    m = MembershipFactory.create(active_from=active_from, active_until=active_until)
    with time_machine.travel(NOW, tick=False):
        assert Membership.objects.active().filter(pk=m.pk).exists() is expected


def test_membership_of_deactivated_user_is_not_active() -> None:
    m = MembershipFactory.create(user=UserFactory.create(is_active=False))
    assert not Membership.objects.active().filter(pk=m.pk).exists()


# --------------------------------------------------------------------------- visible_to()


def test_user_without_active_membership_sees_no_thesis() -> None:
    """Acceptance criterion: no active Membership => no thesis visible."""
    ThesisFactory.create()
    lonely = UserFactory.create()
    ended = MembershipFactory.create(
        active_from=timezone.now() - dt.timedelta(days=2),
        active_until=timezone.now() - dt.timedelta(days=1),
    )
    for user in (lonely, ended.user):
        assert not Thesis.objects.visible_to(user).exists()


def test_superuser_without_membership_sees_no_thesis() -> None:
    ThesisFactory.create()
    admin = UserFactory.create(is_superuser=True, is_staff=True)
    assert not Thesis.objects.visible_to(admin).exists()


def test_visible_to_lists_each_thesis_once() -> None:
    m = MembershipFactory.create(role=Role.DIRECTOR)
    MembershipFactory.create(user=m.user, thesis=m.thesis, role=Role.CO_SUPERVISOR)
    MembershipFactory.create()  # someone else's thesis
    assert list(Thesis.objects.visible_to(m.user)) == [m.thesis]
