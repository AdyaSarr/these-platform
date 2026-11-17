"""M11b: the Monday summary of the objectives (M4) is folded into the activity.

- An approved objective changed, or whose result was declared, is « À faire »
  for the director (VALIDATE_OBJECTIVES): immediate by default; still due while
  not acknowledged. For the other readers it stays an Information.
- An overdue objective becomes the event OBJECTIVE_OVERDUE, written once by the
  daily run, with no author: « Échéances » for the student, Information for the
  other readers.
- No WeeklySummaryLog is written any more; the command is gone; old rows follow
  the 13-month retention.
"""

from __future__ import annotations

import datetime as dt
from types import SimpleNamespace
from typing import Any

import pytest
from django.core import mail

from apps.activity import digests
from apps.activity.models import ActivityNotification, Event
from apps.activity.tests import helpers as h
from apps.core.confidentiality import ConfidentialityLevel as L

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture(autouse=True)
def _now(time_machine: Any) -> None:
    time_machine.move_to(h.NOW, tick=True)


@pytest.fixture
def team() -> SimpleNamespace:
    return h.make_team()


def _approved(team: SimpleNamespace, **kw: Any) -> Any:
    from apps.objectives import services as objectives

    goal = h.objective(team, L.PUBLIC)
    return objectives.approve_objective(objective=goal, user=team.director, recent_mfa=True)


def _to(user: Any) -> list[Any]:
    return [m for m in mail.outbox if user.email in m.to]


def test_a_change_to_acknowledge_is_an_action_for_the_director(team: SimpleNamespace) -> None:
    from apps.objectives import services as objectives

    goal = _approved(team)
    mail.outbox.clear()
    objectives.update_objective(objective=goal, user=team.student, changes={"weight": 3},
                                reason="Ajustement fictif")  # fmt: skip
    assert len(_to(team.director)) == 1  # À faire: immediate by default
    row = ActivityNotification.objects.get(recipient=team.director, kind="EVENT")
    assert row.category == "ACTION"
    assert _to(team.co_supervisor) == []  # an Information, weekly by default
    objectives.acknowledge_changes(objective=goal, user=team.director, recent_mfa=True)
    from apps.activity import notifications

    assert not notifications.still_due(row)  # acknowledged: no longer due


def test_an_overdue_objective_is_written_once(team: SimpleNamespace, time_machine: Any) -> None:
    from apps.objectives import services as objectives

    goal = _approved(team)
    objectives.add_progress(objective=goal, user=team.student, date=h.D(2027, 1, 19),
                            progress=20, status="IN_PROGRESS")  # fmt: skip
    mail.outbox.clear()
    time_machine.move_to(dt.datetime(2027, 7, 1, 6, 0, tzinfo=dt.UTC), tick=True)  # due 30/6
    digests.run()
    digests.run()
    (event,) = Event.objects.filter(kind="OBJECTIVE_OVERDUE")
    assert event.actor_id is None and event.object_id == goal.pk
    assert len(_to(team.student)) == 1  # Échéances: immediate by default
    row = ActivityNotification.objects.get(recipient=team.student, event=event)
    assert row.category == "DEADLINE"


def test_no_weekly_summary_any_more() -> None:
    from django.core.management import get_commands

    assert "objectives_weekly_summary" not in get_commands()


def test_old_summaries_follow_the_retention(team: SimpleNamespace, time_machine: Any) -> None:
    from apps.activity import services
    from apps.objectives.models import WeeklySummaryLog

    old = WeeklySummaryLog.objects.create(
        thesis=team.thesis, recipient=team.director, week_start=h.D(2027, 1, 18)
    )
    time_machine.move_to(dt.datetime(2028, 3, 1, 9, 0, tzinfo=dt.UTC), tick=True)
    services.purge()
    assert not WeeklySummaryLog.objects.filter(pk=old.pk).exists()
