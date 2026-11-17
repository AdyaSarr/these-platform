"""The cards of the thesis dashboard no longer repeat the tiles (frontend step 8a),
and one display of the dashboard opens sealed texts at most once.

The only text the dashboard decrypts: the titles of MY overdue actions (Meetings
card). The next meeting's title is no longer opened (the tile shows its date and
kind) and the budgets left the Travel card (they stay in the Budget tab): one
open_texts at most, hence at most one SEALED_TEXT_READ, with CONFIDENTIAL data.
"""

from __future__ import annotations

import datetime as dt
import re
from types import SimpleNamespace
from typing import Any

import pytest
from django.urls import reverse

from apps.core.confidentiality import ConfidentialityLevel as L
from apps.travel.tests.helpers import NOW, D, client_for, make_team, mission_of

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def team() -> SimpleNamespace:
    return make_team()


def _dashboard(team: SimpleNamespace, role: str) -> str:
    response = client_for(team, role).get(
        reverse("accounts:dashboard", kwargs={"thesis_id": team.thesis.pk})
    )
    assert response.status_code == 200
    return response.content.decode()


def _card(page: str, key: str) -> str | None:
    match = re.search(rf'<section class="block" data-block="{key}">.*?</section>', page, re.S)
    return match.group(0) if match else None


def _confidential_world(team: SimpleNamespace, time_machine: Any) -> Any:
    """A CONFIDENTIAL past meeting with an overdue action of the student, a
    CONFIDENTIAL upcoming meeting, a CONFIDENTIAL mission of this year with an
    estimated expense: before step 8a, a display opened three sets of texts."""
    from apps.meetings import services as meetings
    from apps.meetings.tests.helpers import meeting_of
    from apps.travel import services as travel

    time_machine.move_to(dt.datetime(2027, 1, 5, 9, 0, tzinfo=dt.UTC), tick=False)
    attendees = [team.student, team.director]  # both read CONFIDENTIAL
    past = meeting_of(
        team, level=L.CONFIDENTIAL, title="Réunion fictive confidentielle", attendees=attendees
    )
    meeting_of(
        team,
        level=L.CONFIDENTIAL,
        starts_at=dt.datetime(2027, 1, 27, 13, 0, tzinfo=dt.UTC),
        title="Prochaine réunion fictive confidentielle",
        attendees=attendees,
    )
    time_machine.move_to(dt.datetime(2027, 1, 13, 9, 0, tzinfo=dt.UTC), tick=False)
    action = meetings.add_action(
        meeting=past,
        user=team.student,
        title="Action fictive confidentielle",
        assignee=team.student,
        due_date=D(2027, 1, 15),
    )
    time_machine.move_to(NOW, tick=False)
    mission = mission_of(
        team, destination="ROUSSET", payer="ST", level=L.CONFIDENTIAL,
        start_date=D(2027, 2, 15), end_date=D(2027, 2, 15),
    )  # fmt: skip
    travel.add_expense(mission=mission, user=team.student, category="MEALS", estimated="30")
    travel.set_budget(
        thesis=team.thesis, user=team.tutor, payer="ST", year=2027, amount="1500",
        recent_mfa=True,
    )  # fmt: skip
    return action


def test_one_display_opens_sealed_texts_once(team: SimpleNamespace, time_machine: Any) -> None:
    from apps.vault.models import AuditEvent

    action = _confidential_world(team, time_machine)
    last = AuditEvent.objects.order_by("-seq").values_list("seq", flat=True).first()
    page = _dashboard(team, "student")
    reads = list(AuditEvent.objects.filter(seq__gt=last, action="SEALED_TEXT_READ"))
    assert len(reads) == 1  # the titles of my overdue actions, and nothing else
    assert reads[0].details_json["object_ids"] == [str(action.pk)]
    assert "Action fictive confidentielle" in page
    assert "Prochaine réunion fictive confidentielle" not in page


def test_the_objectives_card_is_gone(team: SimpleNamespace) -> None:
    page = _dashboard(team, "student")
    assert _card(page, "objectives") is None
    assert 'data-tile="objectives"' in page  # the tile stays


def test_no_card_repeats_a_tile(team: SimpleNamespace, time_machine: Any) -> None:
    _confidential_world(team, time_machine)
    page = _dashboard(team, "student")
    admin_file = _card(page, "admin_file")
    assert admin_file is not None and "<progress" not in admin_file
    meetings = _card(page, "meetings")
    assert meetings is not None
    assert "Prochaine réunion" not in meetings and "actions en retard" not in meetings
    assert "Action fictive confidentielle" in meetings  # my overdue actions stay
    conferences = _card(page, "conferences")
    assert conferences is not None and "attend" not in conferences
    travel = _card(page, "travel")
    assert travel is not None
    for repeated in ("Prochaine mission", "attend", "justificatifs", "Budget"):
        assert repeated not in travel, repeated
    assert "Jours au LIRMM" in travel  # not in any tile: stays


def test_budgets_left_the_dashboard_for_the_budget_tab(
    team: SimpleNamespace, time_machine: Any
) -> None:
    _confidential_world(team, time_machine)  # a 2027 ST budget, a committed expense
    page = _dashboard(team, "student")
    assert "engagés sur" not in page
    budget = reverse("travel:budget", kwargs={"thesis_id": team.thesis.pk})
    assert client_for(team, "student").get(budget).status_code == 200
