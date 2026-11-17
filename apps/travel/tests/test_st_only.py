"""The company co-supervisor's view of travel (ST_ONLY): the exact mirror of the
LIRMM co-supervisor's (LIRMM_ONLY). Rousset days and missions to Rousset only;
remote work, conferences, LIRMM days and leave are never shown; never the budget,
the rules, the approvals (« À décider ») nor a decision.

The indistinguishability test of this view is test_minimisation_st.py.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any

import pytest
from django.core.exceptions import PermissionDenied

from apps.accounts.roles import Role
from apps.accounts.tests.factories import MembershipFactory
from apps.travel import services
from apps.travel.access import PresenceAccess, presence_access
from apps.travel.models import Mission, PresenceDay
from apps.travel.tests.helpers import NOW, PASSWORD, D, client_for, make_team, mission_of, url
from apps.travel.tests.test_presence import WEEK, _fill_week
from apps.ui.tests.helpers import without_sidebar
from apps.vault import services as vault

pytestmark = pytest.mark.django_db(transaction=True)

ICS = Role.INDUSTRIAL_CO_SUPERVISOR


@pytest.fixture(autouse=True)
def _now(time_machine: Any) -> None:
    time_machine.move_to(NOW, tick=False)


@pytest.fixture
def team() -> SimpleNamespace:
    team = make_team()
    team.st_co_supervisor = MembershipFactory.create(
        thesis=team.thesis, role=ICS, user__password=PASSWORD
    ).user
    return team


def _missions(team: SimpleNamespace) -> dict[str, Mission]:
    return {
        "lirmm": mission_of(team, destination="LIRMM", payer="LIRMM"),
        "rousset": mission_of(team, destination="ROUSSET", payer="ST"),
        "rousset_lab": mission_of(team, destination="ROUSSET", payer="LIRMM"),
        "conference": mission_of(team, destination="CONFERENCE", payer="LIRMM"),
        "other": mission_of(team, destination="OTHER", destination_detail="Ville", payer="OTHER"),
    }


# --------------------------------------------------------------------------- access


def test_the_access_is_st_only() -> None:
    assert [a.name for a in sorted(PresenceAccess)] == [
        "NONE", "LIRMM_ONLY", "ST_ONLY", "NO_LEAVE", "FULL",
    ]  # fmt: skip


def test_the_company_co_supervisor_gets_st_only(team: SimpleNamespace) -> None:
    assert presence_access(team.st_co_supervisor, team.thesis) is PresenceAccess.ST_ONLY
    assert presence_access(team.co_supervisor, team.thesis) is PresenceAccess.LIRMM_ONLY


def test_the_company_co_supervisor_sees_rousset_days_only(team: SimpleNamespace) -> None:
    _fill_week(team)
    seen = services.visible_days(team.st_co_supervisor, team.thesis, D(2027, 1, 1), D(2027, 1, 31))
    assert seen == {D(2027, 1, 4): "ROUSSET"}
    rows = PresenceDay.objects.visible_to(team.st_co_supervisor, team.thesis)
    assert set(rows.values_list("kind", flat=True)) == {"ROUSSET"}
    assert set(WEEK.values()) - {"ROUSSET"}  # the other kinds exist


def test_the_counter_is_rousset_days_only(team: SimpleNamespace) -> None:
    _fill_week(team)
    services.set_target(
        thesis=team.thesis, user=team.director, recent_mfa=True, cifre_year=1, st_percent=70
    )
    summary = services.split_summary(team.st_co_supervisor, team.thesis)
    assert summary.access is PresenceAccess.ST_ONLY
    assert summary.shown_counts == {"ROUSSET": 1}
    for hidden in (
        "st_days", "lab_days", "lirmm_days", "st_percent", "target_st_percent", "gap",
        "alert", "working_days", "unfilled_days",
    ):  # fmt: skip
        assert getattr(summary, hidden) in (None, 0), hidden


def test_the_company_co_supervisor_sees_missions_to_rousset_only(team: SimpleNamespace) -> None:
    missions = _missions(team)
    listed = {m.pk for m in services.mission_list(team.st_co_supervisor, team.thesis)}
    assert listed == {missions["rousset"].pk, missions["rousset_lab"].pk}
    for hidden in (missions["lirmm"].pk, missions["conference"].pk, uuid.uuid4()):
        with pytest.raises(PermissionDenied):
            services.mission_for(team.st_co_supervisor, team.thesis, hidden, operation="read")


def test_the_texts_of_a_mission_follow_its_level(team: SimpleNamespace) -> None:
    st = mission_of(team, destination="ROUSSET", payer="ST", purpose="Motif ST fictif.")
    lab = mission_of(team, destination="ROUSSET", payer="LIRMM", purpose="Motif labo fictif.")
    assert services.mission_texts(team.st_co_supervisor, st)["purpose"] == "Motif ST fictif."
    assert services.mission_texts(team.st_co_supervisor, lab)["purpose"] is vault.RESTRICTED


def test_the_dashboard_block_is_not_the_full_one(team: SimpleNamespace) -> None:
    from django.test import RequestFactory

    request = RequestFactory().get("/")
    request.user = team.st_co_supervisor
    block = services.dashboard_block(request, team.thesis)
    assert block["full"] is False
    assert block["pending_orders"] == [] and block["pending"] == 0


def test_no_monthly_lirmm_rule(team: SimpleNamespace) -> None:
    """The rule counts LIRMM days, which this viewer does not see: not shown (it
    would always read 0 and "not met"); the LIRMM co-supervisor gets it."""
    services.set_rules(
        thesis=team.thesis, user=team.director, recent_mfa=True, min_lab_days_per_month=3
    )
    assert services.month_rule(team.st_co_supervisor, team.thesis, 2027, 1) is None
    assert services.month_rule(team.co_supervisor, team.thesis, 2027, 1) is not None


def test_no_decision_and_no_budget(team: SimpleNamespace) -> None:
    mission = mission_of(team, destination="ROUSSET", payer="ST")
    services.request_order(mission=mission, user=team.student, estimated_total="1")
    assert services.pending_orders_for(team.st_co_supervisor, team.thesis) == []
    assert services.budget_payers(team.st_co_supervisor, team.thesis) == []


# --------------------------------------------------------------------------- pages


def _month(team: SimpleNamespace) -> str:
    return url(team, "month", year=2027, month=1)


def test_the_pages_show_rousset_only(team: SimpleNamespace) -> None:
    _fill_week(team)
    client = client_for(team, "st_co_supervisor")
    for target in (_month(team), url(team, "split"), url(team, "calendar")):
        response = client.get(target)
        assert response.status_code == 200, target
        html = without_sidebar(response.content.decode())
        assert "Rousset" in html, target
        for word in (
            "Montpellier", "REMOTE", "Télétravail", "CONFERENCE", "Conférence",
            "LEAVE", "Congé", "UNAVAILABLE", "Indisponible",
        ):  # fmt: skip
            assert word not in html, (target, word)


@pytest.mark.parametrize("name", ["budget", "orders", "new_mission"])
def test_the_student_and_deciders_pages_are_refused(team: SimpleNamespace, name: str) -> None:
    assert client_for(team, "st_co_supervisor").get(url(team, name)).status_code == 403


def test_the_rules_are_refused(team: SimpleNamespace) -> None:
    data = {
        "remote_counts_as": "LAB",
        "conference_counts_as": "LAB",
        "whit_monday_holiday": "on",
        "gap_alert_points": "12",
        "min_lab_days_per_month": "",
    }
    assert client_for(team, "st_co_supervisor").post(url(team, "rules"), data).status_code == 403


def test_neither_the_budget_nor_the_approvals_tab(team: SimpleNamespace) -> None:
    html = client_for(team, "st_co_supervisor").get(url(team, "calendar")).content.decode()
    assert url(team, "budget") not in html
    assert url(team, "orders") not in html
    assert url(team, "split") in html  # the tab bar is there


def test_the_settings_page_is_shown_read_only(team: SimpleNamespace) -> None:
    response = client_for(team, "st_co_supervisor").get(url(team, "settings"))
    assert response.status_code == 200
    assert url(team, "rules") not in response.content.decode()


def test_another_thesis_stays_out_of_reach(team: SimpleNamespace) -> None:
    other = make_team()
    PresenceDay.objects.create(thesis=other.thesis, day=D(2027, 1, 4), kind="ROUSSET")
    response = client_for(team, "st_co_supervisor").get(url(other, "calendar"))
    assert response.status_code == 403
