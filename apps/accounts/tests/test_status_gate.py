"""M13a: the thesis status is a condition of permissions.py (docs/modules/M13-fin-de-these.md).

A SECOND, independent transcription of the « Garde de statut » table: a read never
depends on the status; an write is allowed in SETUP and ACTIVE, in DEFENDED only
for the whitelist, never in CLOSED.
"""

from __future__ import annotations

from typing import Any

import pytest

from apps.accounts import permissions
from apps.accounts.models import Thesis
from apps.accounts.permissions import Action, allowed_actions, can
from apps.accounts.roles import Role
from apps.accounts.tests.factories import MembershipFactory, ThesisFactory
from apps.accounts.tests.test_permissions import EXPECTED

A = Action
S = Thesis.Status

READS = {
    A.VIEW_THESIS, A.VIEW_TEAM, A.OBJECTIVE_READ, A.OBJECTIVE_COMMENT_READ, A.MEETING_READ,
    A.CONFERENCE_READ, A.SUBMISSION_READ, A.TRAVEL_READ, A.DOCUMENT_READ,
    A.ADMIN_FILE_READ, A.CIFRE_FILE_READ, A.CSI_FILE_READ, A.CSI_REPORT_READ,
    A.THESIS_EXPORT,  # M13b: the right of access, in every status
}  # fmt: skip
DEFENDED_WRITES = {
    A.DOCUMENT_WRITE, A.DOCUMENT_COMMENT, A.DOCUMENT_GRANT,  # M2
    A.CONFERENCE_WRITE, A.SUBMISSION_WRITE, A.VALIDATE_PUBLICATION,  # M6, ST approvals
    A.TRAVEL_WRITE,  # M7: receipts and actual amounts only (narrowed by travel)
    A.REMOVE_MEMBER,  # M1: end of a membership
    A.DECLARE_DEFENSE, A.REQUEST_CLOSURE,  # the lifecycle itself
    A.SET_ST_CONFIDENTIALITY, A.GRANT_ST_EXPORT,  # M13b: the tutor, until CLOSED
}  # fmt: skip


def expected(role: Role, action: Action, status: str) -> bool:
    if role not in EXPECTED[action]:
        return False
    if action in READS or status in (S.SETUP, S.ACTIVE):
        return True
    return status == S.DEFENDED and action in DEFENDED_WRITES


def test_the_read_actions_are_those_of_the_sheet() -> None:
    assert set(permissions.READ_ACTIONS) == READS


def test_the_writes_by_status_are_those_of_the_sheet() -> None:
    writes = set(Action) - READS
    assert {k: set(v) for k, v in permissions.WRITES_BY_STATUS.items()} == {
        S.SETUP: writes, S.ACTIVE: writes, S.DEFENDED: DEFENDED_WRITES, S.CLOSED: set(),
    }  # fmt: skip


def test_the_lifecycle_actions_and_the_comment_reading() -> None:
    assert {r for r in Role if A.DECLARE_DEFENSE in permissions.ROLE_ACTIONS[r]} == {Role.DIRECTOR}
    assert {r for r in Role if A.REQUEST_CLOSURE in permissions.ROLE_ACTIONS[r]} == {
        Role.DIRECTOR, Role.PHD_STUDENT,
    }  # fmt: skip
    for role in Role:  # the same roles read and write comments; never the CSI member
        held = permissions.ROLE_ACTIONS[role]
        assert (A.OBJECTIVE_COMMENT in held) == (A.OBJECTIVE_COMMENT_READ in held), role
    assert A.OBJECTIVE_COMMENT_READ not in permissions.ROLE_ACTIONS[Role.CSI_MEMBER]


@pytest.mark.django_db
@pytest.mark.parametrize("status", list(S))
@pytest.mark.parametrize("role", list(Role))
def test_can_is_the_role_right_and_the_status_rule(role: Role, status: str) -> None:
    thesis = ThesisFactory.create(status=status)
    user = MembershipFactory.create(thesis=thesis, role=role).user
    for action in Action:
        assert can(user, action, thesis) is expected(role, action, status), (action, status)
    assert allowed_actions(user, thesis) == {a for a in Action if expected(role, a, status)}


@pytest.mark.django_db
def test_the_status_is_read_from_the_database_not_the_object(
    django_assert_num_queries: Any,
) -> None:
    thesis = ThesisFactory.create(status=S.ACTIVE)
    student = MembershipFactory.create(thesis=thesis, role=Role.PHD_STUDENT).user
    Thesis.objects.filter(pk=thesis.pk).update(status=S.CLOSED)
    assert thesis.status == S.ACTIVE  # stale in memory
    with django_assert_num_queries(1):  # roles and status: one query, as before
        assert not can(student, A.DOCUMENT_WRITE, thesis)
    assert can(student, A.DOCUMENT_READ, thesis)
    assert A.DOCUMENT_WRITE not in allowed_actions(student, thesis)


@pytest.mark.django_db
def test_an_object_of_a_closed_thesis_is_refused_too() -> None:
    from apps.vault.models import Document

    thesis = ThesisFactory.create(status=S.CLOSED)
    student = MembershipFactory.create(thesis=thesis, role=Role.PHD_STUDENT).user
    obj = Document(thesis=thesis)
    assert not can(student, A.DOCUMENT_WRITE, obj)
