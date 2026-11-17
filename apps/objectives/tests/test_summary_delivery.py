"""The weekly summary of the objectives goes through core.delivery.deliver_once
(M11a, a gap closed): concerned and still_due are checked again at send time, under
the row's lock, as for every other notification; a skipped row is SKIPPED, logged
with its id and a fixed code only. Its behaviour is unchanged otherwise
(test_summary.py)."""

from __future__ import annotations

import datetime as dt
import importlib
import inspect
import logging
from types import SimpleNamespace
from typing import Any

import pytest
import time_machine
from django.core import mail

from apps.accounts.models import Membership, Thesis
from apps.core.confidentiality import ConfidentialityLevel as L
from apps.objectives import services, summary, tasks
from apps.objectives.models import WeeklySummaryLog
from apps.objectives.tests.helpers import D, make_team, year_of

pytestmark = pytest.mark.django_db(transaction=True)

IN_THE_WEEK = dt.datetime(2027, 3, 10, 10, 0, tzinfo=dt.UTC)
RUN = dt.datetime(2027, 3, 15, 7, 0, tzinfo=dt.UTC)


@pytest.fixture
def team() -> SimpleNamespace:
    return make_team()


def _queued(team: SimpleNamespace, recipient: Any = None) -> WeeklySummaryLog:
    with time_machine.travel(IN_THE_WEEK, tick=False):
        services.create_objective(
            thesis=team.thesis, user=team.student, year=year_of(team), title="Titre fictif",
            category="RESEARCH", due_date=D(2027, 6, 30), level=L.PUBLIC,
        )  # fmt: skip
    with time_machine.travel(RUN, tick=False):
        summary.run()
    return WeeklySummaryLog.objects.get(recipient=recipient or team.director)


def _resend(row: WeeklySummaryLog) -> str:
    WeeklySummaryLog.objects.filter(pk=row.pk).update(status="PENDING", attempts=0, sent_at=None)
    mail.outbox.clear()
    with time_machine.travel(RUN, tick=False):
        tasks.send_weekly_summary(str(row.pk))
    row.refresh_from_db()
    return str(row.status)


def test_the_task_goes_through_the_shared_delivery() -> None:
    source = inspect.getsource(tasks)
    assert "deliver_once(" in source and "send_mail" not in source
    notifications = importlib.import_module("apps.objectives.notifications")
    for name in ("concerned", "still_due", "skip_reason"):
        assert callable(getattr(notifications, name)), name


def test_still_sent_when_due(team: SimpleNamespace) -> None:
    assert _resend(_queued(team)) == "SENT"
    assert len(mail.outbox) == 1


def test_a_former_supervisor_is_skipped(team: SimpleNamespace, caplog: Any) -> None:
    row = _queued(team)
    Membership.objects.filter(user=team.director).update(
        active_until=dt.datetime(2027, 3, 12, tzinfo=dt.UTC)
    )
    caplog.set_level(logging.INFO)
    assert _resend(row) == "SKIPPED" and mail.outbox == []
    assert f"({row.pk}, not_concerned)" in caplog.text


def test_a_finished_thesis_is_skipped(team: SimpleNamespace) -> None:
    row = _queued(team)
    Thesis.objects.filter(pk=team.thesis.pk).update(status=Thesis.Status.DEFENDED)
    assert _resend(row) == "SKIPPED"


def test_nothing_left_for_the_recipient_is_skipped(team: SimpleNamespace) -> None:
    """still_due: what made the summary due is gone (the objective's level is no
    longer readable by the recipient: the tutor does not read INTERNAL_LIRMM)."""
    from apps.objectives.models import Objective

    row = _queued(team, team.tutor)
    Objective.objects.filter(thesis=team.thesis).update(level=L.INTERNAL_LIRMM)
    assert _resend(row) == "SKIPPED"
