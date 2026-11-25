"""The re-registration reminder (M9a): windows J-30 and J-7 in dates of Paris, to
the student only, once; checked again at send time (date changed, thesis finished,
no longer the student); a link only; category « Échéances » (M11b)."""

from __future__ import annotations

import datetime as dt
from types import SimpleNamespace
from typing import Any

import pytest
from django.core import mail
from django.core.management import call_command

from apps.accounts.models import Membership, Thesis
from apps.portfolio import reminders, services
from apps.portfolio.models import PortfolioNotification
from apps.portfolio.tests import helpers as h

pytestmark = pytest.mark.django_db(transaction=True)
DATE = h.D(2027, 2, 26)  # 37 days after h.TODAY


@pytest.fixture
def team(time_machine: Any) -> SimpleNamespace:
    time_machine.move_to(h.NOW, tick=True)
    team = h.make_team()
    services.set_reregistration_date(thesis=team.thesis, user=team.student, day=DATE)
    return team


def _at(day: dt.date) -> dt.datetime:
    # 07:00 in Paris (UTC+1 in winter): the timer's hour.
    return dt.datetime(day.year, day.month, day.day, 6, 0, tzinfo=dt.UTC)


def _run(day: dt.date) -> int:
    return reminders.run(_at(day)).reminders


@pytest.mark.parametrize(
    ("days", "window"),
    [(31, None), (30, "J30"), (8, "J30"), (7, "J7"), (0, "J7"), (-1, None)],
)
def test_the_windows(days: int, window: str | None) -> None:
    assert reminders.window_for(days) == window


def test_one_reminder_per_window_to_the_student_only(team: SimpleNamespace) -> None:
    assert _run(DATE - dt.timedelta(days=31)) == 0
    assert _run(DATE - dt.timedelta(days=30)) == 1
    assert _run(DATE - dt.timedelta(days=29)) == 0  # same window: nothing twice
    assert _run(DATE - dt.timedelta(days=7)) == 1
    assert _run(DATE) == 0
    rows = list(PortfolioNotification.objects.order_by("created_at"))
    assert [(r.window, r.recipient_id) for r in rows] == [("J30", team.student.pk),
                                                          ("J7", team.student.pk)]  # fmt: skip
    assert [m.to for m in mail.outbox] == [[team.student.email], [team.student.email]]


def test_the_e_mail_is_a_link_only(team: SimpleNamespace) -> None:
    h.training(team, title="TEMOIN-FUITE-RAPPEL-FORMATION")
    _run(DATE - dt.timedelta(days=30))
    (message,) = mail.outbox
    assert h.url(team, "trainings") in message.body
    for absent in ("TEMOIN-FUITE-RAPPEL-FORMATION", "26/02", "2027-02-26", team.thesis.title):
        assert absent not in message.body and absent not in message.subject, absent


def test_a_changed_date_is_not_reminded_and_a_new_one_is(team: SimpleNamespace,
                                                         time_machine: Any) -> None:  # fmt: skip
    from apps.portfolio.notifications import skip_reason

    _run(DATE - dt.timedelta(days=30))
    row = PortfolioNotification.objects.get()
    services.set_reregistration_date(thesis=team.thesis, user=team.student,
                                     day=DATE + dt.timedelta(days=10))  # fmt: skip
    assert skip_reason(row) == "not_due"
    assert _run(DATE + dt.timedelta(days=10) - dt.timedelta(days=30)) == 1  # a new event


def test_a_finished_thesis_sends_nothing(team: SimpleNamespace) -> None:
    from apps.portfolio.notifications import skip_reason

    _run(DATE - dt.timedelta(days=30))
    row = PortfolioNotification.objects.get()
    Thesis.objects.filter(pk=team.thesis.pk).update(status="DEFENDED", defended_on=h.TODAY)
    row.refresh_from_db()
    assert skip_reason(row) == "thesis_closed"
    assert _run(DATE - dt.timedelta(days=7)) == 0


def test_a_former_student_is_no_longer_reminded(team: SimpleNamespace) -> None:
    from django.utils import timezone

    from apps.portfolio.notifications import skip_reason

    _run(DATE - dt.timedelta(days=30))
    row = PortfolioNotification.objects.get()
    Membership.objects.filter(user=team.student, thesis=team.thesis).update(
        active_until=timezone.now()
    )
    row.refresh_from_db()
    assert skip_reason(row) == "not_concerned"
    assert _run(DATE - dt.timedelta(days=7)) == 0


def test_a_cleared_date_is_not_reminded(team: SimpleNamespace) -> None:
    from apps.portfolio.notifications import skip_reason

    _run(DATE - dt.timedelta(days=30))
    row = PortfolioNotification.objects.get()
    services.set_reregistration_date(thesis=team.thesis, user=team.student, day=None)
    assert skip_reason(row) == "not_due"


def test_the_reminder_is_a_deadline_of_m11b() -> None:
    from apps.activity.choices import Category
    from apps.activity.routing import CATEGORY_OF, SOURCES

    assert CATEGORY_OF["portfolio"] == {"REREGISTRATION": Category.DEADLINE}
    assert SOURCES["portfolio"].model() is PortfolioNotification


def test_the_command_prints_numbers_only(team: SimpleNamespace, time_machine: Any) -> None:
    import io

    time_machine.move_to(_at(DATE - dt.timedelta(days=30)), tick=False)
    out = io.StringIO()
    call_command("portfolio_reminders", stdout=out)
    assert out.getvalue().strip() == (
        "1 re-registration reminder(s) queued, 0 failed e-mail(s) queued again"
    )
