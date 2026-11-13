"""services.stats_for: rows built from the database, today's date taken in Paris."""

import datetime as dt
from fractions import Fraction
from typing import Any

import pytest
from django.core.exceptions import PermissionDenied

from apps.objectives import services
from apps.objectives.models import Status
from apps.objectives.tests.helpers import D, approved, make_team, objective_of, year_of

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def team(time_machine: Any) -> Any:
    time_machine.move_to(dt.datetime(2027, 3, 1, 10, 0, tzinfo=dt.UTC), tick=False)
    return make_team()


@pytest.mark.parametrize(
    ("instant", "overdue"),
    [
        ("2027-03-14T22:59:00+00:00", 0),  # 23:59 in Paris on the due date: not late yet
        ("2027-03-14T23:30:00+00:00", 1),  # 00:30 in Paris on 15 March: late
    ],
)
def test_overdue_follows_the_date_in_paris(
    team: Any, time_machine: Any, instant: str, overdue: int
) -> None:
    approved(team, due_date=D(2027, 3, 14))
    time_machine.move_to(dt.datetime.fromisoformat(instant), tick=False)
    assert services.stats_for(team.director, team.thesis, year_of(team)).stats.overdue == overdue


def test_stats_of_a_year(team: Any, time_machine: Any) -> None:
    done = approved(team, weight=3)
    half = approved(team, weight=2)
    dropped = approved(team, weight=1)
    objective_of(team)  # a draft
    objective_of(team, year=year_of(team, 2))  # another year: not counted
    services.add_progress(
        objective=done, user=team.student, date=D(2027, 3, 1), progress=90, status=Status.ACHIEVED
    )
    services.add_progress(
        objective=half, user=team.student, date=D(2027, 3, 1), progress=50, status=Status.PARTIAL
    )
    services.drop_objective(objective=dropped, user=team.student, reason="Motif fictif suffisant")
    time_machine.move_to(dt.datetime(2027, 3, 20, 10, 0, tzinfo=dt.UTC), tick=False)
    result = services.stats_for(team.csi, team.thesis, year_of(team))
    assert result.stats.attainment == Fraction(3 * 1 + 2 * Fraction(1, 2), 5)
    assert (result.stats.dropped, result.stats.drafts) == (1, 1)
    assert [p.month for p in result.monthly] == [
        D(2026, 12, 1),
        D(2027, 1, 1),
        D(2027, 2, 1),
        D(2027, 3, 1),
    ]
    # Created on 1 March: nothing before; on 20 March, 3*100 + 2*50 over 5.
    assert [p.value for p in result.monthly] == [None, None, None, Fraction(400, 500)]


@pytest.mark.parametrize("role", ["guest", "outsider"])
def test_without_the_read_right_no_stats(team: Any, role: str) -> None:
    with pytest.raises(PermissionDenied):
        services.stats_for(getattr(team, role), team.thesis, year_of(team))


def test_a_year_of_another_thesis_is_refused(team: Any) -> None:
    other = make_team()
    with pytest.raises(PermissionDenied):
        services.stats_for(team.director, team.thesis, year_of(other))
