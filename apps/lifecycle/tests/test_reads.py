"""Reading is unchanged by the closure (M13a, criterion 7): for each member who
stays, the activity rule, the SQL prefilter, the sealed texts and the comments of
the objectives read the same before and after."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from apps.activity import readers
from apps.activity.models import Event
from apps.core.confidentiality import ConfidentialityLevel as L
from apps.lifecycle.tests import helpers as h

pytestmark = pytest.mark.django_db(transaction=True)


def _world(team: SimpleNamespace) -> SimpleNamespace:
    from apps.activity.tests import helpers as a
    from apps.objectives import services as objectives

    goal = a.objective(team, L.PUBLIC)
    objectives.approve_objective(objective=goal, user=team.director, recent_mfa=True)
    objectives.add_comment(objective=goal, user=team.director, body="Commentaire fictif")
    for level in (L.PUBLIC, L.CONFIDENTIAL, L.INTERNAL_LIRMM):
        a.document(team, level)
    a.submission(team, L.CONFIDENTIAL)
    return SimpleNamespace(goal=goal)


def _reading(user: Any, team: SimpleNamespace, world: SimpleNamespace, ids: set[Any]) -> Any:
    from apps.objectives import services as objectives

    events = [e for e in Event.objects.filter(pk__in=ids)]
    rule = {e.pk for e in events if readers.reads(user, e)}
    prefilter = set(readers.readable_events(user, team.thesis).filter(pk__in=ids)
                    .values_list("pk", flat=True))  # fmt: skip
    comments = [c.body for c in objectives.comments_for(user, world.goal)]
    texts = objectives.objective_detail(user, world.goal).texts
    return rule, prefilter, comments, dict(texts)


@pytest.mark.parametrize("attr", ["student", "director"])
def test_what_one_reads_is_the_same_after_the_closure(attr: str, time_machine: Any) -> None:
    time_machine.move_to(h.NOW, tick=True)
    team = h.make_team()
    world = _world(team)
    ids = set(Event.objects.filter(thesis=team.thesis).values_list("pk", flat=True))
    user = getattr(team, attr)
    before = _reading(user, team, world, ids)
    assert before[0] and before[2] == ["Commentaire fictif"]  # not vacuous
    h.closed(team, time_machine)
    assert _reading(user, team, world, ids) == before
