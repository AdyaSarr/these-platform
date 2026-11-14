"""The two indicators of the objectives page, told apart and computed once.

- « Taux d'atteinte pondéré »: results declared (ACHIEVED 1, PARTIAL 1/2), weighted;
- « Avancement moyen pondéré »: declared progress, weighted. The card and the
  monthly chart call the SAME function (stats.weighted_progress): the card equals
  the chart's point of the current month.
"""

from __future__ import annotations

import datetime as dt
import html
import re
from fractions import Fraction
from typing import Any

import pytest
from django.test import Client
from django.urls import reverse

from apps.objectives import services, stats
from apps.objectives.models import Status
from apps.objectives.tests.helpers import D, approved, make_team

ATTAINMENT_HELP = (
    "Objectifs approuvés déclarés atteints (½ pour partiellement), pondérés par leur poids."
)
PROGRESS_HELP = "Moyenne des pourcentages d'avancement déclarés, pondérée par le poids."


def test_rows_carry_no_progress_of_their_own() -> None:
    """Structural single source: progress comes from the reports (updates) only."""
    assert "progress" not in stats.Row.__dataclass_fields__


def test_the_card_and_the_chart_use_the_same_function(monkeypatch: Any) -> None:
    calls: list[dt.date] = []
    real = stats.weighted_progress

    def counting(rows: Any, day: dt.date) -> Any:
        calls.append(day)
        return real(rows, day)

    monkeypatch.setattr(stats, "weighted_progress", counting)
    row = stats.Row(
        category="RESEARCH",
        weight=2,
        status=Status.IN_PROGRESS,
        due_date=D(2027, 6, 1),
        approved=True,
        created_on=D(2026, 12, 1),
        updates=(stats.Update(D(2027, 1, 5), 40, Status.IN_PROGRESS),),
    )
    today = D(2027, 2, 10)
    card = stats.compute_stats([row], today).progress
    points = stats.monthly_progress(
        [row], first_day=D(2026, 12, 1), last_day=D(2027, 11, 30), today=today
    )
    assert today in calls and points[-1].at in calls
    assert card == points[-1].value == Fraction(40, 100)


@pytest.mark.django_db(transaction=True)
def test_the_card_equals_the_current_month_point(time_machine: Any) -> None:
    time_machine.move_to(dt.datetime(2027, 3, 15, 10, 0, tzinfo=dt.UTC), tick=False)
    team = make_team()
    for weight, progress in ((3, 70), (1, 20), (2, 90)):
        objective = approved(team, weight=weight)
        services.add_progress(
            objective=objective,
            user=team.student,
            date=D(2027, 3, 10),
            progress=progress,
            status=Status.IN_PROGRESS,
        )
    result = services.stats_for(team.director, team.thesis, team.thesis.years.get(number=1))
    assert result.stats.progress == result.monthly[-1].value == Fraction(3 * 70 + 20 + 2 * 90, 600)
    client = Client()
    client.force_login(team.director)
    page = html.unescape(
        client.get(
            reverse("objectives:dashboard", kwargs={"thesis_id": team.thesis.pk})
        ).content.decode()
    )
    card = re.search(r"<dt>Avancement moyen pondéré</dt>\s*<dd>(\d+) %", page)
    last_row = re.findall(r'<th scope="row">[^<]+</th><td>(\d+) %</td>', page)
    assert card is not None and last_row
    assert card.group(1) == last_row[-1] == "68"


@pytest.mark.django_db
def test_the_labels_and_their_help() -> None:
    team = make_team()
    client = Client()
    client.force_login(team.director)
    page = re.sub(
        r"\s+",
        " ",
        html.unescape(
            client.get(
                reverse("objectives:dashboard", kwargs={"thesis_id": team.thesis.pk})
            ).content.decode()
        ),
    )
    assert "<dt>Taux d'atteinte pondéré</dt>" in page and ATTAINMENT_HELP in page
    assert "<dt>Avancement moyen pondéré</dt>" in page and PROGRESS_HELP in page
    assert "Évolution de l'avancement moyen pondéré" in page
    assert "<dt>Avancement pondéré</dt>" not in page  # the old, ambiguous label
