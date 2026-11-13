"""Every (status, event) pair of the product, against an independent table.

An allowed pair moves the item to its target; any other pair raises
InvalidTransitionError and changes NOTHING (status, cycle, approvals, audit log),
even for an actor who holds every right needed by the event.
"""

from __future__ import annotations

import itertools
from types import SimpleNamespace
from typing import Any

import pytest

from apps.admin_file import services
from apps.admin_file.models import ItemApproval, ItemStatus, Reason, RequiredItem
from apps.admin_file.tests.helpers import item_of, make_team, put
from apps.admin_file.transitions import TRANSITIONS, Event, InvalidTransitionError
from apps.vault.models import AuditEvent

pytestmark = pytest.mark.django_db

S, E = ItemStatus, Event

# Written from the M3 plan (section 3), NOT from transitions.py.
EXPECTED: dict[tuple[str, str], str] = {
    ("TODO", "SUBMIT"): "SUBMITTED",
    ("REJECTED", "SUBMIT"): "SUBMITTED",
    ("SUBMITTED", "WITHDRAW"): "TODO",
    ("SUBMITTED", "APPROVE"): "VALIDATED",
    ("SUBMITTED", "REJECT"): "REJECTED",
    ("VALIDATED", "REOPEN"): "TODO",
}

PAIRS = list(itertools.product(list(S), list(E)))


def _fire(event: Event, item: RequiredItem, team: SimpleNamespace) -> RequiredItem:
    """Fire ``event`` with an actor holding every right the event needs."""
    if event is E.SUBMIT:
        return services.submit(item=item, user=team.student)
    if event is E.WITHDRAW:
        return services.withdraw(item=item, user=team.student)
    if event is E.APPROVE:
        return services.approve(item=item, user=team.director)
    if event is E.REJECT:
        return services.reject(item=item, user=team.director, reason=Reason.ILLEGIBLE)
    return services.reopen(item=item, user=team.director, reason=Reason.AMENDMENT)


def _snapshot(item: RequiredItem) -> tuple[Any, ...]:
    item.refresh_from_db()
    return (
        item.status,
        item.cycle,
        item.submitted_by_id,
        item.validated_at,
        item.reason_code,
        ItemApproval.objects.filter(item=item).count(),
        AuditEvent.objects.count(),
    )


def test_the_table_is_the_validated_one() -> None:
    assert {(s.value, e.value): t.value for (s, e), t in TRANSITIONS.items()} == EXPECTED


@pytest.mark.parametrize(("status", "event"), PAIRS, ids=lambda x: str(x))
def test_every_pair(status: ItemStatus, event: Event) -> None:
    team = make_team()
    item = put(item_of(team), status, team.student)
    expected = EXPECTED.get((status.value, event.value))
    if expected is None:
        before = _snapshot(item)
        with pytest.raises(InvalidTransitionError):
            _fire(event, item, team)
        assert _snapshot(item) == before
    else:
        result = _fire(event, item, team)
        item.refresh_from_db()
        assert item.status == result.status == expected


def test_every_status_change_is_audited_with_from_and_to() -> None:
    """Acceptance: each status change creates an AuditEvent (M03 fiche)."""
    team = make_team()
    item = item_of(team)
    steps = [
        (lambda: services.submit(item=item, user=team.student), "ADMIN_ITEM_SUBMITTED"),
        (
            lambda: services.reject(item=item, user=team.director, reason=Reason.ILLEGIBLE),
            "ADMIN_ITEM_REJECTED",
        ),
        (lambda: services.submit(item=item, user=team.student), "ADMIN_ITEM_SUBMITTED"),
        (lambda: services.withdraw(item=item, user=team.student), "ADMIN_ITEM_WITHDRAWN"),
        (lambda: services.submit(item=item, user=team.student), "ADMIN_ITEM_SUBMITTED"),
        (lambda: services.approve(item=item, user=team.director), "ADMIN_ITEM_VALIDATED"),
        (
            lambda: services.reopen(item=item, user=team.student, reason=Reason.AMENDMENT),
            "ADMIN_ITEM_REOPENED",
        ),
    ]
    for run, action in steps:
        before = RequiredItem.objects.get(pk=item.pk).status
        run()
        after = RequiredItem.objects.get(pk=item.pk).status
        event = AuditEvent.objects.filter(action=action).order_by("-seq").first()
        assert event is not None, action
        assert (event.details_json["from"], event.details_json["to"]) == (before, after)
        assert event.object_id == str(item.pk)
        assert event.thesis_id == team.thesis.pk


def test_each_submission_opens_a_new_cycle() -> None:
    team = make_team()
    item = item_of(team)
    services.submit(item=item, user=team.student)
    services.reject(item=item, user=team.director, reason=Reason.ILLEGIBLE)
    services.submit(item=item, user=team.student)
    item.refresh_from_db()
    assert item.cycle == 2
    assert (item.reason_code, item.reason_comment) == ("", "")


def test_reopen_clears_validated_at_and_keeps_the_reason() -> None:
    team = make_team()
    item = item_of(team)
    services.submit(item=item, user=team.student)
    services.approve(item=item, user=team.director)
    services.reopen(
        item=item, user=team.director, reason=Reason.AMENDMENT, comment="Avenant fictif"
    )
    item.refresh_from_db()
    assert item.status == S.TODO
    assert item.validated_at is None
    assert (item.reason_code, item.reason_comment) == ("AMENDMENT", "Avenant fictif")
