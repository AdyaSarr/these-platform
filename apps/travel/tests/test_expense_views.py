"""Views of expenses, receipts and budgets: POSTs like a browser, the help sentence on
receipts, the budget page per role, the budget set with a second factor."""

from __future__ import annotations

import datetime as dt
from html import unescape
from types import SimpleNamespace
from typing import Any

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse

from apps.travel import services
from apps.travel.models import Expense, Mission, TravelBudget
from apps.travel.tests.helpers import (
    NOW,
    ROLES,
    browser_for,
    browser_post,
    client_for,
    make_team,
    mission_of,
    url,
)

pytestmark = pytest.mark.django_db(transaction=True)

RECEIPT_HELP = (
    "Le document est chiffré au niveau de la mission. Évitez d'y laisser des informations "
    "sans rapport (adresse personnelle, autres dépenses)."
)


@pytest.fixture(autouse=True)
def _now(time_machine: Any) -> None:
    time_machine.move_to(NOW, tick=True)


@pytest.fixture
def team() -> SimpleNamespace:
    return make_team()


def _m(team: SimpleNamespace, mission: Mission, name: str = "mission") -> str:
    return url(team, name, mission_id=mission.pk)


def _e(team: SimpleNamespace, expense: Expense, name: str) -> str:
    return url(team, name, expense_id=expense.pk)


def _pdf() -> SimpleUploadedFile:
    return SimpleUploadedFile("f.pdf", b"%PDF-1.7\n%fictif\n%%EOF\n", "application/pdf")


def test_add_a_line_like_a_browser(team: SimpleNamespace) -> None:
    mission = mission_of(team)
    client = browser_for(team, "student")
    response = browser_post(
        client,
        _m(team, mission),
        _m(team, mission, "add_expense"),
        {"category": "MEALS", "estimated": "45", "actual": "", "flat_rate": "on"},
    )
    assert response.status_code == 302, response.content[:500]
    line = Expense.objects.get()
    assert (line.category, line.flat_rate) == ("MEALS", True)


@pytest.mark.parametrize("role", ["director", "co_supervisor", "tutor"])
def test_only_the_student_adds_a_line(team: SimpleNamespace, role: str) -> None:
    mission = mission_of(team)
    response = client_for(team, role).post(
        _m(team, mission, "add_expense"), {"category": "MEALS", "estimated": "1"}
    )
    assert response.status_code == 403


def test_an_invalid_line_is_a_400(team: SimpleNamespace) -> None:
    mission = mission_of(team)
    response = client_for(team, "student").post(
        _m(team, mission, "add_expense"),
        {"category": "TRANSPORT", "estimated": "1", "flat_rate": "on"},
    )
    assert response.status_code == 400
    assert not Expense.objects.exists()


def test_update_amounts_like_a_browser(team: SimpleNamespace) -> None:
    mission = mission_of(team)
    line = services.add_expense(mission=mission, user=team.student, category="TRANSPORT")
    client = browser_for(team, "student")
    response = browser_post(
        client,
        _m(team, mission),
        _e(team, line, "update_expense"),
        {"estimated": "10", "actual": "9,50"},
    )
    assert response.status_code == 302
    (view,) = services.expenses_of(team.student, mission)
    assert (view.estimated, view.actual) == (1000, 950)


def test_receipt_upload_with_its_help(team: SimpleNamespace) -> None:
    mission = mission_of(team)
    line = services.add_expense(mission=mission, user=team.student, category="TRANSPORT")
    page = unescape(client_for(team, "student").get(_m(team, mission)).content.decode())
    assert RECEIPT_HELP in page
    client = browser_for(team, "student")
    response = browser_post(
        client, _m(team, mission), _e(team, line, "attach_receipt"), {"file": _pdf()}
    )
    assert response.status_code == 302, response.content[:500]
    (view,) = services.expenses_of(team.student, mission)
    assert len(view.receipts) == 1


def test_reimbursed_with_a_missing_receipt_is_a_409(
    team: SimpleNamespace, time_machine: Any
) -> None:
    mission = mission_of(team, payer="OTHER")
    services.add_expense(mission=mission, user=team.student, category="TRANSPORT", actual="5")
    time_machine.move_to(dt.datetime(2027, 2, 11, 9, 0, tzinfo=dt.UTC), tick=True)
    services.mark_done(mission=mission, user=team.student)
    response = client_for(team, "student").post(_m(team, mission, "mission_reimbursed"))
    assert response.status_code == 409


@pytest.mark.parametrize("role", ROLES)
def test_budget_page_per_role(team: SimpleNamespace, role: str) -> None:
    expected = 200 if role in ("student", "director", "tutor") else 403
    assert client_for(team, role).get(url(team, "budget")).status_code == expected


def test_budget_set_like_a_browser(team: SimpleNamespace) -> None:
    target = url(team, "set_budget")
    data = {"payer": "ST", "year": "2027", "amount": "4000"}
    assert client_for(team, "director").post(target, data).status_code == 403  # not ST's
    response = client_for(team, "tutor").post(target, data)
    assert response.status_code == 302 and reverse("mfa_reauthenticate") in response["Location"]
    client = browser_for(team, "tutor", mfa=True)
    response = browser_post(client, url(team, "budget"), target, data)
    assert response.status_code == 302, response.content[:500]
    assert TravelBudget.objects.get().payer == "ST"


def test_budget_page_shows_amounts_in_french(team: SimpleNamespace) -> None:
    services.set_budget(
        thesis=team.thesis, user=team.tutor, payer="ST", year=2027, amount="4000", recent_mfa=True
    )
    html = client_for(team, "tutor").get(url(team, "budget") + "?year=2027").content.decode()
    assert "4\u00a0000,00\u00a0€" in html
