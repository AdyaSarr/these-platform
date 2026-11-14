"""Reminders of the conferences: J-30, J-14, J-7, J-1 counted in DATES of the
deadline's own zone (AoE, IANA), request due dates in Paris dates, approvals
awaiting the tutor; once each; a link only; former members get nothing."""

from __future__ import annotations

import datetime as dt
import smtplib
from io import StringIO
from types import SimpleNamespace
from typing import Any

import pytest
from django.core import mail
from django.core.management import call_command

from apps.accounts import services as accounts
from apps.accounts.models import Membership
from apps.conferences import reminders, services, tasks
from apps.conferences.models import ConferenceNotification
from apps.conferences.tests.helpers import (
    NOW,
    D,
    T,
    article_version,
    deadline_of,
    edition_of,
    make_team,
    marker,
    submission_of,
)

pytestmark = pytest.mark.django_db(transaction=True)

UTC = dt.UTC


@pytest.fixture(autouse=True)
def _now(time_machine: Any) -> None:
    time_machine.move_to(NOW, tick=True)


@pytest.fixture
def team() -> SimpleNamespace:
    return make_team()


def _at(*args: int) -> dt.datetime:
    return dt.datetime(*args, tzinfo=UTC)


def _rows(kind: str = "DEADLINE") -> list[ConferenceNotification]:
    return list(ConferenceNotification.objects.filter(kind=kind).order_by("created_at"))


# --------------------------------------------------------------------------- windows


@pytest.mark.parametrize(
    ("days", "window"),
    [(31, None), (30, "J30"), (15, "J30"), (14, "J14"), (8, "J14"), (7, "J7"), (2, "J7"),
     (1, "J1"), (0, "J1"), (-1, None)],
)  # fmt: skip
def test_windows(days: int, window: str | None) -> None:
    assert reminders.window_for(days) == window


def test_counted_in_aoe_dates_not_paris_dates(team: SimpleNamespace) -> None:
    """2027-02-28 23:30 UTC is already 1 March in Paris but still 28 February in AoE."""
    edition = edition_of(team)
    # 2027-03-31 08:00 AoE = 20:00 UTC = 22:00 Paris on 31 March.
    deadline_of(team, edition, day=D(2027, 3, 31), wall=T(8, 0))
    submission_of(team, edition=edition)
    # AoE: 31 days left (no window); Paris dates would say 30 (J-30).
    assert reminders.run(_at(2027, 2, 28, 23, 30)).deadlines == 0
    # The next AoE day: 30 days left.
    assert reminders.run(_at(2027, 3, 1, 12, 30)).deadlines == 1
    assert [r.window for r in _rows()] == ["J30"]


def test_each_window_once_and_a_link_only(team: SimpleNamespace) -> None:
    edition = edition_of(team)
    deadline = deadline_of(team, edition)  # 2027-03-01 23:59 AoE
    title = marker()
    submission_of(team, edition=edition, title=title, authors=[team.tutor])
    mail.outbox.clear()
    # 06:00 UTC is 18:00 the day before in AoE: 30, 28, 14, 7, 2 and 1 days left.
    runs = [_at(2027, 1, 31, 6, 0), _at(2027, 2, 2, 6, 0), _at(2027, 2, 16, 6, 0),
            _at(2027, 2, 23, 6, 0), _at(2027, 2, 28, 6, 0), _at(2027, 3, 1, 6, 0)]  # fmt: skip
    for when in runs:
        reminders.run(when)
        reminders.run(when)  # run again: nothing twice
    assert [r.window for r in _rows() if r.recipient == team.student] == ["J30", "J14", "J7", "J1"]
    assert {r.recipient for r in _rows()} == {team.student, team.tutor}  # co-authors too
    for message in mail.outbox:
        assert title not in message.body and title not in message.subject
        assert f"/conferences/editions/{deadline.edition_id}/" in message.body


def test_a_moved_deadline_is_reminded_again(team: SimpleNamespace) -> None:
    edition = edition_of(team)
    deadline = deadline_of(team, edition)
    submission_of(team, edition=edition)
    reminders.run(_at(2027, 3, 1, 6, 0))  # J-1 (28 February in AoE)
    services.change_deadline(
        deadline=deadline, user=team.student, day=D(2027, 3, 8), wall=T(23, 59), tz_name="AoE"
    )
    reminders.run(_at(2027, 3, 8, 6, 0))  # J-1 of the new date
    assert [r.window for r in _rows() if r.recipient == team.student] == ["J1", "J1"]


def test_only_relevant_deadlines_of_live_submissions(team: SimpleNamespace) -> None:
    edition = edition_of(team)
    deadline_of(team, edition, kind="CAMERA_READY")  # not relevant before acceptance
    deadline_of(team, edition, kind="NOTIFICATION", day=D(2027, 3, 2))
    cancelled = deadline_of(team, edition, day=D(2027, 3, 2))
    services.cancel_deadline(deadline=cancelled, user=team.student)
    submission = submission_of(team, edition=edition)
    assert reminders.run(_at(2027, 2, 28, 6, 0)).deadlines == 0
    deadline_of(team, edition)
    services.set_status(submission=submission, user=team.student, status="WITHDRAWN")
    assert reminders.run(_at(2027, 2, 28, 6, 0)).deadlines == 0
    unrelated = edition_of(team, year=2028)
    deadline_of(team, unrelated)  # no submission aims at it
    assert reminders.run(_at(2027, 2, 28, 6, 0)).deadlines == 0


def test_a_los_angeles_deadline_across_the_us_time_change(team: SimpleNamespace) -> None:
    """2027-03-14: Los Angeles moves to summer time; days are dates in Los Angeles."""
    edition = edition_of(team)
    deadline_of(team, edition, day=D(2027, 3, 15), wall=T(9, 0), tz_name="America/Los_Angeles")
    submission_of(team, edition=edition)
    # 2027-03-14 07:30 UTC is still 13 March in Los Angeles: 2 days left (J-7).
    reminders.run(_at(2027, 3, 14, 7, 30))
    # 2027-03-14 09:00 UTC is 14 March 01:00 PST (summer time starts at 10:00 UTC): 1 day.
    reminders.run(_at(2027, 3, 14, 9, 0))
    assert [r.window for r in _rows()] == ["J7", "J1"]


def test_a_former_member_gets_no_reminder(team: SimpleNamespace) -> None:
    edition = edition_of(team)
    deadline_of(team, edition)
    submission_of(team, edition=edition, authors=[team.tutor])
    accounts.end_membership(
        Membership.objects.active().get(thesis=team.thesis, user=team.tutor), by=team.student
    )
    reminders.run(_at(2027, 2, 28, 6, 0))
    assert {r.recipient for r in _rows()} == {team.student}


# --------------------------------------------------------------------------- ST approval


def test_request_due_reminders_in_paris_dates(team: SimpleNamespace) -> None:
    edition = edition_of(team)
    deadline_of(team, edition)  # request due 2027-01-31 (Paris)
    submission = submission_of(team, edition=edition)
    # 2027-01-23 23:30 UTC is 24 January in Paris: 7 days before the 31st.
    assert reminders.run(_at(2027, 1, 23, 23, 30)).request_dues == 1
    assert [r.window for r in _rows("REQUEST_DUE")] == ["J7"]
    assert _rows("REQUEST_DUE")[0].recipient == team.student
    # Once requested, no more request reminders.
    services.request_approval(
        submission=submission, user=team.student, version=article_version(team, submission)
    )
    assert reminders.run(_at(2027, 1, 30, 6, 0)).request_dues == 0


def test_the_tutor_is_reminded_of_a_pending_approval(team: SimpleNamespace) -> None:
    edition = edition_of(team)
    deadline_of(team, edition)  # 2027-03-01 23:59 AoE
    submission = submission_of(team, edition=edition)
    services.request_approval(
        submission=submission, user=team.student, version=article_version(team, submission)
    )
    reminders.run(_at(2027, 2, 16, 6, 0))  # J-14: not for the tutor
    reminders.run(_at(2027, 2, 23, 6, 0))  # J-7 (22 February in AoE)
    reminders.run(_at(2027, 3, 1, 6, 0))  # J-1
    rows = _rows("APPROVAL_PENDING")
    assert [(r.window, r.recipient) for r in rows] == [("J7", team.tutor), ("J1", team.tutor)]


# --------------------------------------------------------------------------- sending


def test_a_failed_send_is_retried_by_the_daily_run(team: SimpleNamespace, monkeypatch: Any) -> None:
    def refuse(*args: Any, **kwargs: Any) -> None:
        raise smtplib.SMTPException("refused")

    monkeypatch.setattr(tasks, "send_mail", refuse)
    edition = edition_of(team)
    deadline_of(team, edition)
    submission_of(team, edition=edition)
    reminders.run(_at(2027, 2, 28, 6, 0))
    (row,) = _rows()
    assert (row.status, row.attempts) == ("FAILED", 1)
    reminders.run(_at(2027, 3, 1, 6, 0))
    reminders.run(_at(2027, 3, 1, 7, 0))
    row.refresh_from_db()
    assert (row.status, row.attempts) == ("FAILED", 3)


def test_the_command_prints_counts_only(team: SimpleNamespace, time_machine: Any) -> None:
    edition = edition_of(team)
    deadline_of(team, edition)
    submission_of(team, edition=edition, title="Titre fictif à ne pas afficher")
    time_machine.move_to(_at(2027, 2, 28, 6, 0))
    out = StringIO()
    call_command("conferences_reminders", stdout=out)
    text = out.getvalue()
    assert text.startswith("1 deadline reminder(s)")
    assert "Titre" not in text and "@" not in text


def test_run_refuses_a_naive_instant() -> None:
    with pytest.raises(ValueError):
        reminders.run(dt.datetime(2027, 1, 1))
