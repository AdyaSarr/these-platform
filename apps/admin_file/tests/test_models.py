"""Database constraints of the administrative file: the last line of defence,
independent of the services (which will check the same rules first)."""

from __future__ import annotations

import datetime as dt
from typing import Any

import pytest
from django.db import IntegrityError, transaction
from django.db.models import ProtectedError
from django.utils import timezone

from apps.accounts.roles import Role
from apps.accounts.tests.factories import UserFactory
from apps.admin_file.models import ItemApproval, RequiredItem
from apps.admin_file.tests.factories import ItemApprovalFactory, RequiredItemFactory

pytestmark = pytest.mark.django_db

S = RequiredItem.Status
D, T = Role.DIRECTOR, Role.INDUSTRIAL_TUTOR


def _refused(**kwargs: Any) -> None:
    with pytest.raises(IntegrityError), transaction.atomic():
        RequiredItemFactory.create(**kwargs)


def _approval_refused(**kwargs: Any) -> None:
    with pytest.raises(IntegrityError), transaction.atomic():
        ItemApprovalFactory.create(**kwargs)


# --------------------------------------------------------------------------- RequiredItem


@pytest.mark.parametrize("roles", [[D], [T], [D, T]])
def test_allowed_validator_sets(roles: list[Role]) -> None:
    RequiredItemFactory.create(validator_roles=roles)


@pytest.mark.parametrize(
    "roles",
    [
        [],
        [Role.PHD_STUDENT],
        [Role.CO_SUPERVISOR],
        [Role.CSI_MEMBER],
        [D, D],  # a duplicate would make the item impossible to validate
        [T, D],  # one spelling per set
        [D, T, D],
        [D, Role.CO_SUPERVISOR],
    ],
)
def test_other_validator_sets_are_refused(roles: list[Role]) -> None:
    _refused(validator_roles=roles)


def test_one_item_per_code_and_thesis() -> None:
    item = RequiredItemFactory.create(code="adum_enrollment")
    _refused(thesis=item.thesis, code="adum_enrollment")
    RequiredItemFactory.create(code="adum_enrollment")  # another thesis: fine


@pytest.mark.parametrize("code", ["", "Adum", "adum-enrollment", "1adum", "custom-0123456789ab"])
def test_catalogue_codes_have_no_label_and_a_strict_shape(code: str) -> None:
    _refused(code=code)


def test_custom_items_need_a_label_and_a_custom_code() -> None:
    RequiredItemFactory.create(code="custom-0123456789ab", label="Renouvellement fictif")
    _refused(code="custom-0123456789ab", label="")
    _refused(code="custom-xyz", label="Renouvellement fictif")
    _refused(code="adum_enrollment", label="Libellé interdit sur une pièce du catalogue")


def test_unknown_level_section_or_status_is_refused() -> None:
    _refused(default_level="SECRET")
    _refused(section="HR")
    _refused(status="LOST")


def test_validated_iff_validated_at() -> None:
    now = timezone.now()
    user = UserFactory.create(mfa=False)
    RequiredItemFactory.create(status=S.VALIDATED, validated_at=now, cycle=1, submitted_by=user)
    _refused(status=S.VALIDATED, validated_at=None, cycle=1, submitted_by=user)
    _refused(status=S.TODO, validated_at=now)


def test_rejected_needs_a_reason() -> None:
    user = UserFactory.create(mfa=False)
    RequiredItemFactory.create(
        status=S.REJECTED, reason_code="ILLEGIBLE", cycle=1, submitted_by=user
    )
    _refused(status=S.REJECTED, reason_code="", cycle=1, submitted_by=user)
    _refused(reason_code="WHATEVER")


@pytest.mark.parametrize("status", [S.SUBMITTED, S.VALIDATED, S.REJECTED])
def test_a_submitted_item_has_a_submitter_and_a_cycle(status: str) -> None:
    extra: dict[str, Any] = {"validated_at": timezone.now()} if status == S.VALIDATED else {}
    if status == S.REJECTED:
        extra["reason_code"] = "OTHER"
    _refused(status=status, cycle=0, submitted_by=UserFactory.create(mfa=False), **extra)
    _refused(status=status, cycle=1, submitted_by=None, **extra)


def test_due_date_is_optional() -> None:
    assert RequiredItemFactory.create(due_date=None).due_date is None
    assert RequiredItemFactory.create(due_date=dt.date(2027, 1, 1)).due_date == dt.date(2027, 1, 1)


def test_str_never_shows_the_label() -> None:
    item = RequiredItemFactory.create(code="custom-0123456789ab", label="Libellé fictif")
    assert "Libellé" not in str(item)


# --------------------------------------------------------------------------- ItemApproval


def test_one_approval_per_role_and_cycle() -> None:
    first = ItemApprovalFactory.create(role=D)
    _approval_refused(item=first.item, cycle=1, role=D, submitted_by=first.submitted_by)
    ItemApprovalFactory.create(item=first.item, cycle=2, role=D, submitted_by=first.submitted_by)


def test_one_approval_per_person_and_cycle_even_with_two_roles() -> None:
    """Separation of duties: a DIRECTOR + INDUSTRIAL_TUTOR cannot approve twice."""
    first = ItemApprovalFactory.create(
        role=D, item__validator_roles=[D, T], item__code="collaboration_contract"
    )
    _approval_refused(
        item=first.item, cycle=1, role=T, user=first.user, submitted_by=first.submitted_by
    )
    ItemApprovalFactory.create(item=first.item, cycle=1, role=T, submitted_by=first.submitted_by)


def test_the_submitter_never_approves() -> None:
    someone = UserFactory.create(mfa=False)
    _approval_refused(user=someone, submitted_by=someone)


@pytest.mark.parametrize(
    "role", [Role.PHD_STUDENT, Role.CO_SUPERVISOR, Role.CSI_MEMBER, Role.GUEST]
)
def test_approval_roles_are_validator_roles(role: Role) -> None:
    _approval_refused(role=role)


def test_approval_cycle_starts_at_one() -> None:
    _approval_refused(cycle=0)


def test_approvals_are_protected_by_their_item() -> None:
    approval = ItemApprovalFactory.create()
    with pytest.raises(ProtectedError), transaction.atomic():
        approval.item.delete()
    assert ItemApproval.objects.filter(pk=approval.pk).exists()
