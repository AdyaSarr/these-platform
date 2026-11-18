"""SR-01 (revue 2026-11, docs/security/revue-2026-11.md): the lists and the dashboard
of M4, M5a and M6 reveal objects the viewer cannot read (date, type, duration,
counts), whereas docs/decisions.md (2026-11-17) states that « un co-encadrant LIRMM
n'apprend pas qu'une réunion INTERNAL_ST est planifiée ».

Two worlds that differ only by objects at a level the viewer does not read, the
pages rendered at the same frozen instant: they should be identical. They are
not: these tests are expected to fail (xfail strict) until the finding is fixed.
"""

from __future__ import annotations

import datetime as dt
import re
from typing import Any

import pytest
from django.urls import reverse

from apps.activity.tests import helpers as a
from apps.core.confidentiality import ConfidentialityLevel as L

pytestmark = pytest.mark.django_db(transaction=True)
CSRF = re.compile(r'(name="csrfmiddlewaretoken" value=|"X-CSRFToken": )"[^"]+"')
FIXED = dt.datetime(2027, 1, 22, 9, 0, tzinfo=dt.UTC)


def _pages(user: Any, thesis: Any, names: tuple[str, ...]) -> dict[str, str]:
    client = a.client_for(user)
    out = {}
    for name in names:
        response = client.get(reverse(name, kwargs={"thesis_id": thesis.pk}))
        assert response.status_code == 200, name  # never compare an error page
        out[name] = CSRF.sub(r'\1"-"', response.content.decode())
    return out


@pytest.mark.xfail(strict=True, reason="SR-01: a meeting at an unreadable level is listed")
def test_sr01_the_lirmm_co_supervisor_does_not_learn_of_an_st_meeting(time_machine: Any) -> None:
    time_machine.move_to(a.NOW, tick=True)
    team = a.make_team()
    a.meeting(team, L.PUBLIC, days=3, attendees=[team.student, team.co_supervisor])
    names = ("accounts:dashboard", "meetings:list")
    time_machine.move_to(FIXED, tick=False)
    before = _pages(team.co_supervisor, team.thesis, names)
    assert "agenda-item" in before["accounts:dashboard"]  # not vacuous: the shared meeting
    time_machine.move_to(FIXED - dt.timedelta(hours=1), tick=True)
    a.meeting(team, L.INTERNAL_ST, days=2, attendees=[team.student])  # not readable by him
    time_machine.move_to(FIXED, tick=False)
    assert _pages(team.co_supervisor, team.thesis, names) == before


@pytest.mark.xfail(strict=True, reason="SR-01: counts include objects at unreadable levels")
def test_sr01_the_tutor_does_not_count_an_unreadable_objective(time_machine: Any) -> None:
    time_machine.move_to(a.NOW, tick=True)
    team = a.make_team()
    a.objective(team, L.PUBLIC)
    names = ("accounts:dashboard", "objectives:dashboard")
    time_machine.move_to(FIXED, tick=False)
    before = _pages(team.tutor, team.thesis, names)
    assert 'data-drafts="1"' in before["objectives:dashboard"]  # not vacuous
    time_machine.move_to(FIXED - dt.timedelta(hours=1), tick=True)
    a.objective(team, L.INTERNAL_LIRMM)  # not readable by the tutor
    time_machine.move_to(FIXED, tick=False)
    assert _pages(team.tutor, team.thesis, names) == before
