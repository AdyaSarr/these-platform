"""The monthly rule (at least N days at the LIRMM): never "not met" before the month (a
Paris date) is over. In progress: the LIRMM days so far (today included) and the
LIRMM days planned for the rest of the month (presence filled in, missions to the
LIRMM planned, requested or granted (a refused one does not count, but presence days
filled in always do), each working day counted once. One function for the month page
and the dashboard card; the co-supervisor gets the same figures (LIRMM only)."""

from __future__ import annotations

import datetime as dt
from html import unescape
from types import SimpleNamespace
from typing import Any

import pytest
from django.urls import reverse

from apps.travel import services
from apps.travel.models import PresenceDay
from apps.travel.tests.helpers import NOW, D, client_for, make_team, mission_of, url

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture(autouse=True)
def _now(time_machine: Any) -> None:
    time_machine.move_to(NOW, tick=False)  # Wednesday 20 January 2027


@pytest.fixture
def team() -> SimpleNamespace:
    team = make_team()
    services.set_rules(
        thesis=team.thesis, user=team.director, recent_mfa=True, min_lab_days_per_month=3
    )
    return team


def _day(team: SimpleNamespace, day: dt.date, kind: str = "LIRMM") -> None:
    PresenceDay.objects.create(thesis=team.thesis, day=day, kind=kind)


def _january(team: SimpleNamespace) -> None:
    _day(team, D(2027, 1, 5))  # past
    _day(team, D(2027, 1, 20))  # today: done
    _day(team, D(2027, 1, 23))  # a Saturday: not a working day
    _day(team, D(2027, 1, 26))  # future, also a mission day: counted once
    mission_of(team, start_date=D(2027, 1, 26), end_date=D(2027, 1, 28))  # 26, 27, 28
    cancelled = mission_of(team, start_date=D(2027, 1, 29), end_date=D(2027, 1, 29))
    services.cancel_mission(mission=cancelled, user=team.student)
    mission_of(team, destination="ROUSSET", start_date=D(2027, 1, 25), end_date=D(2027, 1, 25))


def test_in_progress_counts_done_and_planned(team: SimpleNamespace) -> None:
    _january(team)
    rule = services.month_rule(team.student, team.thesis, 2027, 1)
    assert rule is not None
    assert (rule.minimum, rule.done, rule.planned) == (3, 2, 3)
    assert rule.finished is False and rule.met is None  # never "not met" while running


def test_a_finished_month_is_met_or_not(team: SimpleNamespace) -> None:
    _day(team, D(2026, 12, 8))
    december = services.month_rule(team.student, team.thesis, 2026, 12)
    assert december is not None
    assert (december.finished, december.done, december.met) == (True, 1, False)
    for day in (D(2026, 12, 9), D(2026, 12, 10)):
        _day(team, day)
    december = services.month_rule(team.student, team.thesis, 2026, 12)
    assert december is not None and december.met is True


def test_a_month_to_come_is_all_planned(team: SimpleNamespace) -> None:
    mission_of(team, start_date=D(2027, 2, 8), end_date=D(2027, 2, 10))
    rule = services.month_rule(team.student, team.thesis, 2027, 2)
    assert rule is not None
    assert (rule.done, rule.planned, rule.finished, rule.met) == (0, 3, False, None)


def test_the_last_day_of_the_month_is_still_in_progress(
    team: SimpleNamespace, time_machine: Any
) -> None:
    """31 January 23:30 in Paris is 22:30 UTC: the month is not over yet."""
    time_machine.move_to(dt.datetime(2027, 1, 31, 22, 30, tzinfo=dt.UTC), tick=False)
    rule = services.month_rule(team.student, team.thesis, 2027, 1)
    assert rule is not None and rule.finished is False
    time_machine.move_to(dt.datetime(2027, 1, 31, 23, 30, tzinfo=dt.UTC), tick=False)
    rule = services.month_rule(team.student, team.thesis, 2027, 1)
    assert rule is not None and rule.finished is True  # 1 February in Paris


def _refused(team: SimpleNamespace, day: dt.date, *, fill_presence: bool) -> None:
    mission = mission_of(
        team, payer="ST", start_date=day, end_date=day, fill_presence=fill_presence
    )
    order = services.request_order(mission=mission, user=team.student, estimated_total="1")
    services.decide_order(
        order=order, user=team.tutor, decision="REFUSED", reason="OTHER", recent_mfa=True
    )


def test_a_refused_approval_is_not_planned(team: SimpleNamespace) -> None:
    _refused(team, D(2027, 1, 27), fill_presence=False)
    rule = services.month_rule(team.student, team.thesis, 2027, 1)
    assert rule is not None and rule.planned == 0


def test_presence_filled_in_counts_even_from_a_refused_mission(team: SimpleNamespace) -> None:
    """The presence calendar is what counts: the student corrects it if needed."""
    _refused(team, D(2027, 1, 27), fill_presence=True)
    assert PresenceDay.objects.get(thesis=team.thesis, day=D(2027, 1, 27)).kind == "LIRMM"
    rule = services.month_rule(team.student, team.thesis, 2027, 1)
    assert rule is not None and rule.planned == 1


@pytest.mark.parametrize(
    ("payer", "steps", "planned"),
    [
        ("ST", [], 1),  # PLANNED
        ("ST", ["request"], 1),  # ORDER_REQUESTED
        ("ST", ["request", "grant"], 1),  # ORDER_GRANTED
        ("OTHER", [], 1),  # another body: no approval, PLANNED
    ],
)
def test_missions_to_the_lirmm_still_planned(
    team: SimpleNamespace, payer: str, steps: list[str], planned: int
) -> None:
    mission = mission_of(team, payer=payer, start_date=D(2027, 1, 27), end_date=D(2027, 1, 27))
    if "request" in steps:
        order = services.request_order(mission=mission, user=team.student, estimated_total="1")
        if "grant" in steps:
            services.decide_order(order=order, user=team.tutor, decision="GRANTED", recent_mfa=True)
    rule = services.month_rule(team.student, team.thesis, 2027, 1)
    assert rule is not None and rule.planned == planned


def test_no_rule_no_figure(team: SimpleNamespace) -> None:
    services.set_rules(
        thesis=team.thesis, user=team.director, recent_mfa=True, min_lab_days_per_month=None
    )
    assert services.month_rule(team.student, team.thesis, 2027, 1) is None


def test_the_co_supervisor_gets_the_same_figures(team: SimpleNamespace) -> None:
    _january(team)
    assert services.month_rule(team.co_supervisor, team.thesis, 2027, 1) == services.month_rule(
        team.student, team.thesis, 2027, 1
    )


# =========================================================================== shown


def _html(response: Any) -> str:
    return unescape(response.content.decode())


@pytest.mark.parametrize("role", ["student", "co_supervisor"])
def test_month_page_in_progress(team: SimpleNamespace, role: str) -> None:
    _january(team)
    html = _html(client_for(team, role).get(url(team, "month", year=2027, month=1)))
    assert "En cours : 2 jours au LIRMM, 3 prévus ; règle : au moins 3" in html
    assert "Non respectée" not in html


def test_month_page_singular(team: SimpleNamespace) -> None:
    _day(team, D(2027, 1, 20))
    _day(team, D(2027, 1, 27))
    html = _html(client_for(team, "student").get(url(team, "month", year=2027, month=1)))
    assert "En cours : 1 jour au LIRMM, 1 prévu ; règle : au moins 3" in html


def test_month_page_of_a_finished_month(team: SimpleNamespace) -> None:
    _day(team, D(2026, 12, 8))
    html = _html(client_for(team, "student").get(url(team, "month", year=2026, month=12)))
    assert "Non respectée" in html and "En cours" not in html


def test_the_card_uses_the_same_rule(team: SimpleNamespace) -> None:
    _january(team)
    html = _html(
        client_for(team, "co_supervisor").get(
            reverse("accounts:dashboard", kwargs={"thesis_id": team.thesis.pk})
        )
    )
    assert "En cours : 2 jours au LIRMM, 3 prévus ; règle : au moins 3" in html
    assert "Non respectée" not in html


# =========================================================================== gap unit


def test_the_gap_is_shown_in_points(team: SimpleNamespace) -> None:
    # 2 Rousset days, 1 LIRMM day: ST share 66.7 %, target 50: gap 16.7 points.
    for day in (D(2027, 1, 4), D(2027, 1, 5)):
        _day(team, day, "ROUSSET")
    _day(team, D(2027, 1, 6))
    services.set_target(
        thesis=team.thesis, user=team.tutor, recent_mfa=True, cifre_year=1, st_percent=50
    )
    card = _html(
        client_for(team, "student").get(
            reverse("accounts:dashboard", kwargs={"thesis_id": team.thesis.pk})
        )
    )
    split = _html(client_for(team, "student").get(url(team, "split")))
    assert "16,7 points" in card
    assert "16,7 points" in split
