"""SETUP -> ACTIVE: only when EVERY mandatory item is VALIDATED, atomically with the
last validation, audited once, even when the last two validations race."""

from __future__ import annotations

import contextlib
import threading
from types import SimpleNamespace
from typing import Any

import pytest

from apps.accounts.models import Thesis
from apps.admin_file import services
from apps.admin_file.models import ItemApproval, ItemStatus, Reason, RequiredItem
from apps.admin_file.tests.helpers import item_of, make_team
from apps.vault import audit as audit_module
from apps.vault.errors import AuditError
from apps.vault.models import AuditEvent
from apps.vault.tests.concurrency import run_threads

S = ItemStatus


def _status(team: SimpleNamespace) -> str:
    return Thesis.objects.get(pk=team.thesis.pk).status


def _activations() -> int:
    return AuditEvent.objects.filter(action="THESIS_ACTIVATED").count()


def _validate(team: SimpleNamespace, item: RequiredItem) -> None:
    services.submit(item=item, user=team.student)
    services.approve(item=item, user=team.director)


@pytest.mark.django_db
def test_active_only_when_every_mandatory_item_is_validated() -> None:
    team = make_team()
    first, second = item_of(team), item_of(team)
    optional = item_of(team, mandatory=False)
    _validate(team, first)
    assert _status(team) == Thesis.Status.SETUP  # one mandatory item left
    _validate(team, second)
    assert _status(team) == Thesis.Status.ACTIVE  # the optional one is still TODO
    assert RequiredItem.objects.get(pk=optional.pk).status == S.TODO
    event = AuditEvent.objects.get(action="THESIS_ACTIVATED")
    assert event.thesis_id == team.thesis.pk
    assert event.actor_id == team.director.pk
    assert event.details_json == {"from": "SETUP", "to": "ACTIVE"}


@pytest.mark.django_db
def test_a_submitted_or_rejected_mandatory_item_blocks_activation() -> None:
    team = make_team()
    first, second, third = item_of(team), item_of(team), item_of(team)
    services.submit(item=first, user=team.student)
    services.submit(item=second, user=team.student)
    services.reject(item=second, user=team.director, reason=Reason.ILLEGIBLE)
    _validate(team, third)
    services.approve(item=first, user=team.director)
    assert _status(team) == Thesis.Status.SETUP


@pytest.mark.django_db
def test_a_custom_mandatory_item_counts() -> None:
    team = make_team()
    catalogue = item_of(team)
    custom = item_of(team, code="custom-0123456789ab", label="Renouvellement fictif")
    _validate(team, catalogue)
    assert _status(team) == Thesis.Status.SETUP
    _validate(team, custom)
    assert _status(team) == Thesis.Status.ACTIVE


@pytest.mark.django_db
def test_a_partial_approval_does_not_activate() -> None:
    team = make_team()
    item = item_of(team, section="CIFRE", validator_roles=["DIRECTOR", "INDUSTRIAL_TUTOR"])
    services.submit(item=item, user=team.student)
    services.approve(item=item, user=team.director)
    assert _status(team) == Thesis.Status.SETUP
    services.approve(item=item, user=team.tutor)
    assert _status(team) == Thesis.Status.ACTIVE


@pytest.mark.django_db
def test_reopening_keeps_the_thesis_active_and_says_so() -> None:
    team = make_team()
    item = item_of(team)
    _validate(team, item)
    services.reopen(item=item, user=team.student, reason=Reason.AMENDMENT)
    assert _status(team) == Thesis.Status.ACTIVE
    event = AuditEvent.objects.get(action="ADMIN_ITEM_REOPENED")
    assert event.details_json["mandatory"] is True
    _validate(team, item)
    assert _activations() == 1  # never twice


@pytest.mark.django_db
def test_an_active_thesis_is_not_reactivated() -> None:
    team = make_team(status=Thesis.Status.ACTIVE)
    _validate(team, item_of(team))
    assert _activations() == 0


@pytest.mark.django_db
def test_a_thesis_without_mandatory_item_is_not_activated() -> None:
    team = make_team()
    _validate(team, item_of(team, mandatory=False))
    assert _status(team) == Thesis.Status.SETUP


@pytest.mark.django_db
def test_activation_is_atomic_with_the_last_validation(monkeypatch: pytest.MonkeyPatch) -> None:
    """If the THESIS_ACTIVATED event cannot be written, nothing of the validation stays."""
    team = make_team()
    item = item_of(team)
    services.submit(item=item, user=team.student)
    real_audit = audit_module.audit

    def failing(action: Any, **kwargs: Any) -> Any:
        if action == "THESIS_ACTIVATED":
            raise AuditError("injected failure")
        return real_audit(action, **kwargs)

    monkeypatch.setattr(audit_module, "audit", failing)
    monkeypatch.setattr(services, "audit", failing)
    with pytest.raises(AuditError):
        services.approve(item=item, user=team.director)
    item.refresh_from_db()
    assert item.status == S.SUBMITTED
    assert item.validated_at is None
    assert not ItemApproval.objects.filter(item=item).exists()
    assert _status(team) == Thesis.Status.SETUP
    assert not AuditEvent.objects.filter(action="ADMIN_ITEM_VALIDATED").exists()


@pytest.mark.django_db(transaction=True)
@pytest.mark.serial
def test_racing_last_validations_activate_exactly_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """Two directors validate the last two mandatory items at the same time.

    Both transactions are held at the activation check by a barrier. With the
    thesis row lock, the second one waits for the first to commit, the barrier
    times out, and the second then sees both items VALIDATED. Without the lock,
    both read before either commits: neither would activate.
    """
    team = make_team()
    first, second = item_of(team), item_of(team)
    services.submit(item=first, user=team.student)
    services.submit(item=second, user=team.student)
    barrier = threading.Barrier(2, timeout=2)
    real_check = services._all_mandatory_validated

    def held(thesis: Thesis) -> bool:
        with contextlib.suppress(threading.BrokenBarrierError):
            barrier.wait()
        return real_check(thesis)

    monkeypatch.setattr(services, "_all_mandatory_validated", held)

    def approve_first() -> None:
        services.approve(item=first, user=team.director)

    def approve_second() -> None:
        services.approve(item=second, user=team.director)

    run_threads(approve_first, approve_second, control=lambda: None)
    assert _status(team) == Thesis.Status.ACTIVE
    assert _activations() == 1
