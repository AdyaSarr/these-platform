"""The digests keep every guarantee of the former Monday summary of the objectives
(apps/objectives/tests/test_summary.py, removed with it in M11b): periods on
dates in Paris, once per period and recipient, the database's refusals, retries up
to the limit, a link only, ids only through the broker and in the logs, an SMTP
server that never answers, the checks at send time.
"""

from __future__ import annotations

import datetime as dt
import inspect
import io
import logging
import smtplib
import socket
import threading
import time
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any
from unittest import mock

import pytest
from django.core import mail
from django.core.management import call_command
from django.db import IntegrityError, connection, transaction
from django.utils import timezone

from apps.accounts.models import Membership, Thesis
from apps.activity import digests
from apps.activity.models import ActivityNotification
from apps.activity.notifications import MAX_ATTEMPTS, period
from apps.activity.tests import helpers as h
from apps.core.confidentiality import ConfidentialityLevel as L

pytestmark = pytest.mark.django_db(transaction=True)

MONDAY = dt.datetime(2027, 1, 25, 6, 0, tzinfo=dt.UTC)  # 07:00 in Paris
WEEKLY = "DIGEST_WEEKLY"


@pytest.fixture
def team(time_machine: Any) -> SimpleNamespace:
    time_machine.move_to(h.NOW, tick=True)  # Wednesday 20 January
    return h.make_team()


def _goal(team: SimpleNamespace, level: L = L.PUBLIC) -> Any:
    """An objective of the week: an Information for the supervisors (weekly)."""
    return h.objective(team, level)


def _weekly(user: Any) -> ActivityNotification:
    return ActivityNotification.objects.get(recipient=user, kind=WEEKLY)


# --------------------------------------------------------------------------- periods


@pytest.mark.parametrize(
    ("instant", "monday"),
    [
        ("2027-03-15T07:00:00+00:00", dt.date(2027, 3, 15)),  # Monday morning
        ("2027-03-14T23:30:00+00:00", dt.date(2027, 3, 15)),  # Monday 00:30 in Paris
        ("2027-03-28T22:30:00+00:00", dt.date(2027, 3, 29)),  # Monday 00:30 (UTC+2)
        ("2027-10-31T23:00:00+00:00", dt.date(2027, 11, 1)),  # Monday 00:00 (UTC+1)
    ],
)
def test_the_weekly_digest_runs_on_the_monday_of_the_date_in_paris(
    instant: str, monday: dt.date, time_machine: Any
) -> None:
    time_machine.move_to(dt.datetime.fromisoformat(instant), tick=False)
    due = digests._due(timezone.localdate())
    assert (WEEKLY, monday - dt.timedelta(days=7)) in [(str(k), s) for k, _f, s in due]


@pytest.mark.parametrize(
    ("start", "hours"),
    [
        (dt.date(2027, 3, 22), 167),  # the week summer time starts (28 March)
        (dt.date(2027, 10, 25), 169),  # the week winter time starts (31 October)
        (dt.date(2027, 1, 18), 168),
    ],
)
def test_a_week_is_seven_dates_in_paris(start: dt.date, hours: int) -> None:
    first, end = period(WEEKLY, start)
    assert end.astimezone(dt.UTC) - first.astimezone(dt.UTC) == dt.timedelta(hours=hours)
    assert timezone.localtime(first).hour == 0 and timezone.localtime(end).hour == 0


def test_a_period_refuses_a_datetime() -> None:
    with pytest.raises(TypeError):
        period(WEEKLY, dt.datetime(2027, 1, 18, tzinfo=dt.UTC))


# --------------------------------------------------------------------------- who, when


@pytest.mark.parametrize("change", ["created", "changed", "progress", "comment"])
def test_every_kind_of_objective_activity_counts(
    team: SimpleNamespace, change: str, time_machine: Any
) -> None:
    from apps.objectives import services as objectives

    goal = _goal(team) if change == "created" else None
    if change != "created":
        time_machine.move_to(h.NOW - dt.timedelta(days=14), tick=True)  # an older objective
        goal = _goal(team)
        from apps.activity.services import mark_all_read

        for user in (team.director, team.co_supervisor, team.tutor):
            mark_all_read(user, team.thesis)
        time_machine.move_to(h.NOW, tick=True)
        if change == "changed":
            objectives.update_objective(objective=goal, user=team.student,
                                        changes={"weight": 3})  # fmt: skip
        elif change == "progress":
            objectives.add_progress(objective=goal, user=team.student, date=h.D(2027, 1, 19),
                                    progress=10, status="IN_PROGRESS")  # fmt: skip
        else:
            objectives.add_comment(objective=goal, user=team.co_supervisor, body="Avis fictif")
    time_machine.move_to(MONDAY, tick=True)
    digests.run()
    assert _weekly(team.director).status == "SENT"


def test_former_members_deactivated_users_and_finished_theses(
    team: SimpleNamespace, time_machine: Any
) -> None:
    _goal(team)
    Membership.objects.filter(user=team.co_supervisor).update(active_until=h.NOW)
    type(team.tutor).objects.filter(pk=team.tutor.pk).update(is_active=False)
    other = h.make_team()
    _goal(other)
    Thesis.objects.filter(pk=other.thesis.pk).update(status=Thesis.Status.DEFENDED)
    mail.outbox.clear()
    time_machine.move_to(MONDAY, tick=True)
    digests.run()
    recipients = {a for m in mail.outbox for a in m.to}
    assert team.director.email in recipients
    for user in (team.co_supervisor, team.tutor, other.director):
        assert user.email not in recipients


def test_once_per_period_and_recipient(team: SimpleNamespace, time_machine: Any) -> None:
    _goal(team)
    time_machine.move_to(MONDAY, tick=True)
    digests.run()
    digests.run()
    assert ActivityNotification.objects.filter(recipient=team.director, kind=WEEKLY).count() == 1


def test_the_database_refuses_a_second_digest_and_a_week_not_starting_on_monday(
    team: SimpleNamespace,
) -> None:
    def make(start: dt.date, user: Any) -> None:
        ActivityNotification.objects.create(
            thesis=team.thesis, recipient=user, kind=WEEKLY, period_start=start
        )

    make(dt.date(2027, 1, 18), team.director)
    with pytest.raises(IntegrityError), transaction.atomic():
        make(dt.date(2027, 1, 18), team.director)
    with pytest.raises(IntegrityError), transaction.atomic():
        make(dt.date(2027, 1, 19), team.tutor)  # a Tuesday


def test_the_company_co_supervisor_gets_the_weekly_digest(
    team: SimpleNamespace, time_machine: Any
) -> None:
    """Was: the Monday summary reached the company co-supervisor."""
    _goal(team)
    time_machine.move_to(MONDAY, tick=True)
    digests.run()
    assert _weekly(team.st_co_supervisor).status == "SENT"


# --------------------------------------------------------------------------- sending


def test_a_failed_sending_is_retried_up_to_the_limit(
    team: SimpleNamespace, time_machine: Any
) -> None:
    _goal(team)
    with mock.patch("apps.core.delivery.send_mail", side_effect=OSError("down")):
        for minutes in (0, 10, 20, 30):
            time_machine.move_to(MONDAY + dt.timedelta(minutes=minutes), tick=True)
            digests.run()
    row = _weekly(team.director)
    assert (row.status, row.attempts) == ("FAILED", MAX_ATTEMPTS)
    mail.outbox.clear()
    digests.run()
    assert [m for m in mail.outbox if team.director.email in m.to] == []  # no fourth attempt


def test_the_e_mail_is_a_link_only(team: SimpleNamespace, settings: Any, time_machine: Any) -> None:
    from django.urls import reverse

    _goal(team, L.PUBLIC)
    mail.outbox.clear()
    time_machine.move_to(MONDAY, tick=True)
    digests.run()
    row = _weekly(team.director)
    link = settings.SITE_URL + reverse(
        "activity:digest", kwargs={"thesis_id": team.thesis.pk, "digest_id": row.pk}
    )
    (message,) = [m for m in mail.outbox if team.director.email in m.to]
    assert message.to == [team.director.email] and not message.cc and not message.bcc
    assert message.subject == "Votre résumé d'activité sur la plateforme"
    assert message.body == (
        "Votre résumé d'activité vous attend sur la plateforme\u00a0:\n\n"
        f"{link}\n\n"
        "Ce message ne contient aucun détail\u00a0: connectez-vous pour le consulter.\n"
    )
    for secret in ("TEMOIN-FUITE", team.thesis.title, team.student.last_name):
        assert secret not in message.subject + message.body


def test_the_task_takes_the_id_only_and_stores_no_result() -> None:
    from apps.activity.tasks import send_activity_notification

    assert list(inspect.signature(send_activity_notification.run).parameters) == ["notification_id"]
    assert not getattr(send_activity_notification, "autoretry_for", ())
    assert send_activity_notification.ignore_result


def test_only_the_id_goes_through_the_broker(team: SimpleNamespace, time_machine: Any) -> None:
    _goal(team)
    time_machine.move_to(MONDAY, tick=True)
    with mock.patch("apps.activity.tasks.send_activity_notification.delay") as delay:
        digests.run()
    ids = sorted(str(pk) for pk in ActivityNotification.objects.filter(kind=WEEKLY)
                 .values_list("pk", flat=True))  # fmt: skip
    assert ids and sorted(call.args[0] for call in delay.call_args_list) == ids
    assert all(len(call.args) == 1 and not call.kwargs for call in delay.call_args_list)


@pytest.mark.parametrize("smtp_fails", [False, True])
def test_logs_and_output_hold_no_title_address_or_body(
    team: SimpleNamespace, caplog: pytest.LogCaptureFixture, smtp_fails: bool, time_machine: Any
) -> None:
    _goal(team)
    caplog.clear()
    caplog.set_level(logging.DEBUG)

    def fake_send_mail(subject: str, body: str, *args: Any, **kwargs: Any) -> int:
        if smtp_fails:
            raise smtplib.SMTPException(f"server echoed TEMOIN-FUITE {team.director.email}")
        return 1

    out = io.StringIO()
    time_machine.move_to(MONDAY, tick=True)
    with mock.patch("apps.core.delivery.send_mail", side_effect=fake_send_mail):
        call_command("activity_digests", stdout=out)
    captured = " ".join(f"{r.getMessage()} {r.args}" for r in caplog.records) + out.getvalue()
    for secret in ("TEMOIN-FUITE", team.director.email, team.thesis.title, "résumé"):
        assert secret not in captured
    assert out.getvalue().strip().split()[0].isdigit()  # counts only


SILENCE = 30.0


@pytest.fixture
def silent_smtp() -> Iterator[int]:
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    accepted: list[socket.socket] = []

    def serve() -> None:
        server.settimeout(SILENCE)
        try:
            conn, _ = server.accept()
        except OSError:
            return
        accepted.append(conn)
        time.sleep(SILENCE)  # never sends the 220 greeting
        conn.close()

    threading.Thread(target=serve, daemon=True).start()
    yield server.getsockname()[1]
    server.close()
    for conn in accepted:
        conn.close()


@pytest.mark.serial
def test_a_stuck_smtp_server_times_out_and_releases_the_row(
    team: SimpleNamespace, settings: Any, silent_smtp: int, time_machine: Any
) -> None:
    settings.EMAIL_BACKEND = "django.core.mail.backends.smtp.EmailBackend"
    settings.EMAIL_HOST = "127.0.0.1"
    settings.EMAIL_PORT = silent_smtp
    settings.EMAIL_USE_TLS = False
    settings.EMAIL_TIMEOUT = 1  # 10 in base and prod
    _goal(team, L.INTERNAL_LIRMM)
    for user in (team.co_supervisor, team.st_co_supervisor, team.csi, team.student):
        Membership.objects.filter(user=user).update(active_until=h.NOW)  # one e-mail
    time_machine.move_to(MONDAY, tick=True)
    started = time.monotonic()
    digests.run()
    assert time.monotonic() - started < 10  # gave up after EMAIL_TIMEOUT, not SILENCE
    row = _weekly(team.director)
    assert (row.status, row.attempts, row.sent_at) == ("FAILED", 1, None)
    assert not connection.in_atomic_block


# --------------------------------------------------------------------------- send time


def _held_digest(team: SimpleNamespace, time_machine: Any, monkeypatch: Any) -> str:
    from apps.activity import tasks

    held: list[str] = []
    monkeypatch.setattr(tasks.send_activity_notification, "delay", held.append)
    _goal(team)
    time_machine.move_to(MONDAY, tick=True)
    digests.run()
    return str(_weekly(team.director).pk)


def test_a_former_supervisor_is_skipped(
    team: SimpleNamespace, time_machine: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from apps.activity import tasks

    row_id = _held_digest(team, time_machine, monkeypatch)
    Membership.objects.filter(user=team.director).update(active_until=timezone.now())
    tasks.send_activity_notification(row_id)
    assert ActivityNotification.objects.get(pk=row_id).status == "SKIPPED"


def test_a_finished_thesis_is_skipped(
    team: SimpleNamespace, time_machine: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from apps.activity import tasks

    row_id = _held_digest(team, time_machine, monkeypatch)
    Thesis.objects.filter(pk=team.thesis.pk).update(status=Thesis.Status.DEFENDED)
    tasks.send_activity_notification(row_id)
    assert ActivityNotification.objects.get(pk=row_id).status == "SKIPPED"


def test_nothing_left_is_skipped(
    team: SimpleNamespace, time_machine: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The objective's level is no longer read by the recipient (the tutor does
    not read INTERNAL_LIRMM)."""
    from apps.activity import tasks
    from apps.objectives.models import Objective

    held: list[str] = []
    monkeypatch.setattr(tasks.send_activity_notification, "delay", held.append)
    _goal(team)
    time_machine.move_to(MONDAY, tick=True)
    digests.run()
    row_id = str(_weekly(team.tutor).pk)
    Objective.objects.filter(thesis=team.thesis).update(level=L.INTERNAL_LIRMM)
    tasks.send_activity_notification(row_id)
    assert ActivityNotification.objects.get(pk=row_id).status == "SKIPPED"
