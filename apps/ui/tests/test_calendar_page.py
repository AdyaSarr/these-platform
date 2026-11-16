"""Presence calendar (frontend step 2, point 10).

- The sentence « N jours ouvrés au LIRMM ce mois-ci » is gone: it repeated the
  monthly rule's line and could read as contradicting it (the rule counts what
  the rule counts). The counter itself stays in travel.services (month_view).
- Seven columns of equal width (table-layout: fixed), the table in a scrolling
  wrapper on a narrow screen, never the whole page.
"""

from __future__ import annotations

import html as html_module
import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from django.urls import reverse

from apps.travel import services
from apps.travel.models import PresenceDay
from apps.travel.tests.helpers import NOW, D, client_for, make_team

pytestmark = pytest.mark.django_db(transaction=True)

BASE_DIR = Path(__file__).resolve().parents[3]


@pytest.fixture(autouse=True)
def _now(time_machine: Any) -> None:
    time_machine.move_to(NOW, tick=False)


@pytest.fixture
def team() -> SimpleNamespace:
    team = make_team()
    PresenceDay.objects.create(thesis=team.thesis, day=D(2027, 1, 5), kind="LIRMM")
    services.set_rules(
        thesis=team.thesis, user=team.director, recent_mfa=True, min_lab_days_per_month=3
    )
    return team


def _month(team: SimpleNamespace, role: str) -> str:
    url = reverse("travel:month", kwargs={"thesis_id": team.thesis.pk, "year": 2027, "month": 1})
    response = client_for(team, role).get(url)
    assert response.status_code == 200
    return response.content.decode()


@pytest.mark.parametrize("role", ["student", "director", "co_supervisor", "tutor"])
def test_no_count_sentence_under_the_calendar(team: SimpleNamespace, role: str) -> None:
    page = _month(team, role)
    assert "au LIRMM ce mois-ci" not in page
    assert "LIRMM" in page  # the day itself is still shown


def test_the_monthly_rule_stays(team: SimpleNamespace) -> None:
    text = html_module.unescape(re.sub(r"<[^>]+>", " ", _month(team, "student")))
    assert re.search(r"au moins\s3", text)


def test_the_calendar_scrolls_inside_its_wrapper(team: SimpleNamespace) -> None:
    page = _month(team, "student")
    assert re.search(r'<div class="table-wrap">\s*<table class="table calendar">', page)


def test_seven_columns_of_equal_width() -> None:
    css = (BASE_DIR / "static" / "css" / "components.css").read_text(encoding="utf-8")
    calendar = re.findall(r"\.calendar\s*\{([^}]*)\}", css)
    assert calendar and re.search(r"table-layout\s*:\s*fixed", calendar[0])
    # No width on a single column: fixed layout then shares the width equally.
    for selector, body in re.findall(r"(\.calendar[^{]*)\{([^}]*)\}", css):
        if re.search(r"\b(th|td|col)\b", selector):
            assert not re.search(r"(?<![-\w])width\s*:", body), selector
