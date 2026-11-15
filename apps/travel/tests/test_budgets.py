"""Yearly budgets per payer (M7, step 5; the conference budget moved here from M6): the
ST budget is the tutor's, the LIRMM budget the director's (second factor after the
rights, journaled without any amount), sealed at the payer's level. The summary shows
the budget, the committed (estimates) and actual amounts, and the conference share;
only the student, the tutor (ST) and the director (LIRMM) see it: the LIRMM total
adds missions a co-supervisor does not see."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from django.core.exceptions import PermissionDenied, ValidationError

from apps.travel import services
from apps.travel.models import TravelBudget
from apps.travel.tests.helpers import NOW, D, database_dump, make_team, mission_of
from apps.vault.errors import ReauthenticationRequiredError
from apps.vault.models import AuditEvent

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture(autouse=True)
def _now(time_machine: Any) -> None:
    time_machine.move_to(NOW, tick=True)


@pytest.fixture
def team() -> SimpleNamespace:
    return make_team()


def _set(team: SimpleNamespace, role: str, payer: str, amount: str, year: int = 2027) -> Any:
    return services.set_budget(
        thesis=team.thesis,
        user=getattr(team, role),
        payer=payer,
        year=year,
        amount=amount,
        recent_mfa=True,
    )


def _events() -> list[dict[str, Any]]:
    return [
        e.details_json
        for e in AuditEvent.objects.filter(action="TRAVEL_BUDGET_SET").order_by("seq")
    ]


# =========================================================================== setting


@pytest.mark.parametrize(
    ("role", "payer", "level"),
    [("tutor", "ST", "INTERNAL_ST"), ("director", "LIRMM", "INTERNAL_LIRMM")],
)
def test_each_payers_person_sets_its_budget(
    team: SimpleNamespace, role: str, payer: str, level: str
) -> None:
    _set(team, role, payer, "4500")
    _set(team, role, payer, "4800,50")
    budget = TravelBudget.objects.get(thesis=team.thesis, payer=payer, year=2027)
    assert budget.level == level
    assert "4800.50" not in "\n".join(database_dump())
    summary = services.budget_summary(team.student, team.thesis, 2027)
    assert summary[payer].budget == 480050
    assert _events() == [
        {"payer": payer, "year": 2027, "created": True},
        {"payer": payer, "year": 2027, "created": False},
    ]


@pytest.mark.parametrize(
    ("role", "payer"),
    [("director", "ST"), ("tutor", "LIRMM"), ("student", "ST"), ("co_supervisor", "LIRMM")],
)
def test_nobody_else_sets_a_budget(team: SimpleNamespace, role: str, payer: str) -> None:
    with pytest.raises(PermissionDenied):  # before any second factor
        services.set_budget(
            thesis=team.thesis,
            user=getattr(team, role),
            payer=payer,
            year=2027,
            amount="10",
            recent_mfa=False,
        )
    assert not TravelBudget.objects.exists()


def test_a_budget_asks_for_the_second_factor(team: SimpleNamespace) -> None:
    with pytest.raises(ReauthenticationRequiredError):
        services.set_budget(
            thesis=team.thesis,
            user=team.tutor,
            payer="ST",
            year=2027,
            amount="10",
            recent_mfa=False,
        )
    assert not TravelBudget.objects.exists()


@pytest.mark.parametrize(
    ("payer", "year", "amount"),
    [("OTHER", 2027, "10"), ("ST", 2025, "10"), ("ST", 2031, "10"), ("ST", 2027, "1e3")],
)
def test_invalid_budgets(team: SimpleNamespace, payer: str, year: int, amount: str) -> None:
    role = "tutor"
    with pytest.raises((ValidationError, PermissionDenied)):
        services.set_budget(
            thesis=team.thesis,
            user=getattr(team, role),
            payer=payer,
            year=year,
            amount=amount,
            recent_mfa=True,
        )
    assert not TravelBudget.objects.exists()


# =========================================================================== summary


def _spend(team: SimpleNamespace, estimated: str, actual: str = "", **kw: Any) -> Any:
    mission = mission_of(team, **kw)
    services.add_expense(
        mission=mission, user=team.student, category="TRANSPORT", estimated=estimated, actual=actual
    )
    return mission


def test_the_summary_adds_the_years_missions(team: SimpleNamespace) -> None:
    _set(team, "director", "LIRMM", "3000")
    _spend(team, "200", "180", payer="LIRMM")
    _spend(team, "900", payer="LIRMM", destination="CONFERENCE")
    cancelled = _spend(team, "5000", payer="LIRMM")
    services.cancel_mission(mission=cancelled, user=team.student)
    _spend(team, "70", payer="LIRMM", start_date=D(2026, 12, 14), end_date=D(2026, 12, 15))
    _spend(team, "999", payer="ST")  # another payer
    line = services.budget_summary(team.director, team.thesis, 2027)["LIRMM"]
    assert (line.budget, line.committed, line.actual) == (300000, 110000, 18000)
    assert (line.conference_committed, line.conference_actual) == (90000, 0)
    assert services.budget_summary(team.director, team.thesis, 2026)["LIRMM"].committed == 7000


def test_a_refused_approval_commits_nothing(team: SimpleNamespace) -> None:
    mission = _spend(team, "400", payer="ST")
    order = services.request_order(mission=mission, user=team.student, estimated_total="400")
    services.decide_order(
        order=order, user=team.tutor, decision="REFUSED", reason="BUDGET_EXHAUSTED", recent_mfa=True
    )
    assert services.budget_summary(team.tutor, team.thesis, 2027)["ST"].committed == 0


@pytest.mark.parametrize(
    ("role", "payers"),
    [("student", {"ST", "LIRMM"}), ("tutor", {"ST"}), ("director", {"LIRMM"})],
)
def test_who_sees_which_budget(team: SimpleNamespace, role: str, payers: set[str]) -> None:
    assert set(services.budget_summary(getattr(team, role), team.thesis, 2027)) == payers


@pytest.mark.parametrize("role", ["co_supervisor", "csi", "guest", "outsider"])
def test_no_budget_for_the_others(team: SimpleNamespace, role: str) -> None:
    with pytest.raises(PermissionDenied):
        services.budget_summary(getattr(team, role), team.thesis, 2027)


def test_no_budget_set_shows_none(team: SimpleNamespace) -> None:
    line = services.budget_summary(team.student, team.thesis, 2027)["ST"]
    assert line.budget is None and line.committed == 0
