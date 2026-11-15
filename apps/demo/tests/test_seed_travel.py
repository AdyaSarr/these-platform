"""demo_seed, travel (M7): fictitious presence, missions to the LIRMM and to a fictitious
conference of M6, approvals granted, pending and refused, fictitious amounts sealed,
an official order and receipts in the vault; written through the services only, each
at its simulated instant (the chain and the timestamps are checked by test_seed.py)."""

from __future__ import annotations

from typing import Any

import pytest
from django.utils import timezone

from apps.accounts.models import Membership, Thesis, User
from apps.accounts.roles import Role
from apps.demo import scenario
from apps.travel import services
from apps.travel.models import (
    Expense,
    ExpenseReceipt,
    Mission,
    MissionOrder,
    PresenceDay,
    TravelBudget,
)

pytestmark = [pytest.mark.django_db(transaction=True), pytest.mark.without_vault_keys]


@pytest.fixture
def demo(settings: Any) -> scenario.SeedResult:
    settings.DEBUG = True
    return scenario.seed()


def _thesis() -> Thesis:
    return Thesis.objects.get(title=scenario.DEMO_TITLE)


def _user(role: Role) -> User:
    return Membership.objects.active().get(thesis=_thesis(), role=role).user


def test_presence_over_the_months(demo: scenario.SeedResult) -> None:
    kinds = set(PresenceDay.objects.filter(thesis=_thesis()).values_list("kind", flat=True))
    assert {"ROUSSET", "LIRMM", "REMOTE", "LEAVE"} <= kinds
    assert PresenceDay.objects.filter(thesis=_thesis()).count() > 60
    summary = services.split_summary(_user(Role.PHD_STUDENT), _thesis(), cifre_year=1)
    assert summary.target_st_percent is not None and summary.st_percent is not None


def test_missions_in_every_state_the_story_needs(demo: scenario.SeedResult) -> None:
    thesis = _thesis()
    statuses = sorted(Mission.objects.filter(thesis=thesis).values_list("status", flat=True))
    assert statuses == ["DONE", "ORDER_REFUSED", "ORDER_REQUESTED", "REIMBURSED"]
    decisions = sorted(
        MissionOrder.objects.filter(thesis=thesis).values_list("decision", flat=True)
    )
    assert decisions == ["GRANTED", "GRANTED", "PENDING", "REFUSED"]
    conference = Mission.objects.get(thesis=thesis, destination="CONFERENCE")
    assert conference.edition is not None and conference.submission is not None
    reimbursed = Mission.objects.get(thesis=thesis, status="REIMBURSED")
    assert reimbursed.official_order_document_id is not None
    pending = MissionOrder.objects.get(thesis=thesis, decision="PENDING")
    assert pending.mission.start_date > timezone.localdate()  # still to come


def test_amounts_are_sealed_and_add_up(demo: scenario.SeedResult) -> None:
    thesis = _thesis()
    # One budget per payer and per calendar year the demonstration covers.
    payers = set(TravelBudget.objects.filter(thesis=thesis).values_list("payer", flat=True))
    assert payers == {"ST", "LIRMM"}
    assert ExpenseReceipt.objects.filter(expense__mission__thesis=thesis).exists()
    assert Expense.objects.filter(thesis=thesis, flat_rate=True).exists()
    year = Mission.objects.get(thesis=thesis, status="REIMBURSED").start_date.year
    lines = services.budget_summary(_user(Role.PHD_STUDENT), thesis, year)
    assert lines["LIRMM"].budget is not None and lines["LIRMM"].actual > 0


def test_the_co_supervisor_sees_lirmm_missions_only(demo: scenario.SeedResult) -> None:
    seen = services.mission_list(_user(Role.CO_SUPERVISOR), _thesis())
    assert seen and {m.destination for m in seen} == {"LIRMM"}
    assert len(seen) < Mission.objects.filter(thesis=_thesis()).count()


def test_a_mission_reminder_is_due_today(demo: scenario.SeedResult) -> None:
    """The DONE conference mission lacks a receipt: the card and the reminder show it."""
    done = Mission.objects.get(thesis=_thesis(), status="DONE")
    assert services.expenses_incomplete(done)
