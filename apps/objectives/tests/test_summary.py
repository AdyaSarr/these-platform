"""Weekly summary to the supervisors: a link only, sent when there is something
for THIS recipient (on the objectives they may read), once per week and recipient."""

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
from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any
from unittest import mock

import pytest
import time_machine
from django.core import mail
from django.core.management import call_command
from django.db import IntegrityError, connection, transaction
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import Membership, Thesis
from apps.core.confidentiality import ConfidentialityLevel as L
from apps.objectives import services, summary
from apps.objectives.models import Objective, Status, WeeklySummaryLog
from apps.objectives.tests.helpers import D, make_team, marker, year_of
from apps.vault.tests.concurrency import run_threads

pytestmark = pytest.mark.django_db(transaction=True)

# Monday 2027-03-15, 08:00 in Paris (the timer); the summarised week is 8-14 March.
RUN = "2027-03-15T07:00:00+00:00"
IN_THE_WEEK = dt.datetime(2027, 3, 10, 10, 0, tzinfo=dt.UTC)
WEEK_BEFORE = dt.datetime(2027, 3, 3, 10, 0, tzinfo=dt.UTC)


@contextmanager
def _at(instant: str | dt.datetime) -> Iterator[None]:
    when = dt.datetime.fromisoformat(instant) if isinstance(instant, str) else instant
    with time_machine.travel(when, tick=False):
        yield


def _run(instant: str = RUN) -> dict[str, int]:
    with _at(instant):
        return summary.run()


@pytest.fixture
def team() -> SimpleNamespace:
    with _at(dt.datetime(2027, 1, 4, 9, 0, tzinfo=dt.UTC)):
        return make_team()


def _objective(team: SimpleNamespace, when: dt.datetime, **overrides: Any) -> Objective:
    fields: dict[str, Any] = {
        "thesis": team.thesis,
        "user": team.student,
        "year": year_of(team),
        "title": "Titre fictif",
        "category": "RESEARCH",
        "due_date": D(2027, 6, 30),
        "level": L.PUBLIC,
    }
    fields.update(overrides)
    with _at(when):
        return services.create_objective(**fields)


def _recipients() -> list[str]:
    return sorted(address for m in mail.outbox for address in m.to)


# --------------------------------------------------------------------------- week


@pytest.mark.parametrize(
    ("instant", "monday"),
    [
        ("2027-03-15T07:00:00+00:00", D(2027, 3, 15)),  # Monday morning
        ("2027-03-14T22:59:00+00:00", D(2027, 3, 8)),  # Sunday 23:59 in Paris (UTC+1)
        ("2027-03-14T23:30:00+00:00", D(2027, 3, 15)),  # Monday 00:30 in Paris
        # Summer time starts on Sunday 2027-03-28.
        ("2027-03-28T21:59:00+00:00", D(2027, 3, 22)),  # Sunday 23:59 (UTC+2)
        ("2027-03-28T22:30:00+00:00", D(2027, 3, 29)),  # Monday 00:30 (UTC+2)
        # Winter time starts on Sunday 2027-10-31.
        ("2027-10-31T22:59:00+00:00", D(2027, 10, 25)),  # Sunday 23:59 (UTC+1)
        ("2027-10-31T23:00:00+00:00", D(2027, 11, 1)),  # Monday 00:00 (UTC+1)
    ],
)
def test_week_start_is_the_monday_of_the_date_in_paris(instant: str, monday: dt.date) -> None:
    with _at(instant):
        assert summary.week_start() == monday


def test_week_start_refuses_datetimes() -> None:
    with pytest.raises(TypeError):
        summary.week_start(dt.datetime(2027, 3, 15, 7, 0, tzinfo=dt.UTC))


def test_the_summarised_week_is_the_seven_days_before_monday_in_paris() -> None:
    start, end = summary.window(D(2027, 3, 29))  # Monday after the summer-time change
    assert start == dt.datetime(2027, 3, 21, 23, 0, tzinfo=dt.UTC)  # Mon 22 00:00 (UTC+1)
    assert end == dt.datetime(2027, 3, 28, 22, 0, tzinfo=dt.UTC)  # Mon 29 00:00 (UTC+2)


# --------------------------------------------------------------------------- recipients


def test_supervisors_receive_it_when_there_was_activity(team: SimpleNamespace) -> None:
    _objective(team, IN_THE_WEEK)
    counts = _run()
    assert _recipients() == sorted(
        [team.director.email, team.co_supervisor.email, team.tutor.email]
    )
    assert counts == {"theses": 1, "queued": 3}
    # Never the student, the CSI member or the guest.
    for user in (team.student, team.csi, team.guest, team.outsider):
        assert user.email not in _recipients()


def test_nothing_without_activity_overdue_or_pending(team: SimpleNamespace) -> None:
    _objective(team, WEEK_BEFORE)  # activity, but the week before
    assert _run() == {"theses": 1, "queued": 0}
    assert mail.outbox == []


def test_the_tutor_gets_nothing_from_an_internal_lirmm_objective(team: SimpleNamespace) -> None:
    _objective(team, IN_THE_WEEK, level=L.INTERNAL_LIRMM)
    _run()
    assert _recipients() == sorted([team.director.email, team.co_supervisor.email])


def test_the_co_supervisor_gets_nothing_from_a_confidential_objective(
    team: SimpleNamespace,
) -> None:
    _objective(team, IN_THE_WEEK, level=L.CONFIDENTIAL)
    _run()
    assert _recipients() == sorted([team.director.email, team.tutor.email])


@pytest.mark.parametrize("kind", ["revision", "progress", "comment"])
def test_every_kind_of_activity_counts(team: SimpleNamespace, kind: str) -> None:
    objective = _objective(team, WEEK_BEFORE)
    with _at(WEEK_BEFORE + dt.timedelta(hours=1)):
        objective = services.approve_objective(
            objective=objective, user=team.director, recent_mfa=True
        )
    with _at(IN_THE_WEEK):
        if kind == "revision":
            services.update_objective(
                objective=objective,
                user=team.student,
                changes={"weight": 3},
                reason="Motif fictif suffisant",
            )
            services.acknowledge_changes(objective=objective, user=team.director, recent_mfa=True)
        elif kind == "progress":
            services.add_progress(
                objective=objective,
                user=team.student,
                date=D(2027, 3, 10),
                progress=20,
                status=Status.IN_PROGRESS,
            )
        else:
            services.add_comment(objective=objective, user=team.tutor, body="Avis fictif")
    _run()
    assert len(mail.outbox) == 3, kind


def test_an_overdue_objective_alone_is_enough(team: SimpleNamespace) -> None:
    objective = _objective(team, WEEK_BEFORE, due_date=D(2027, 3, 12), level=L.INTERNAL_LIRMM)
    with _at(WEEK_BEFORE + dt.timedelta(hours=1)):
        services.approve_objective(objective=objective, user=team.director, recent_mfa=True)
    _run()
    assert _recipients() == sorted([team.director.email, team.co_supervisor.email])


def test_a_pending_acknowledgement_reaches_the_director_only(team: SimpleNamespace) -> None:
    objective = _objective(team, WEEK_BEFORE)
    with _at(WEEK_BEFORE + dt.timedelta(hours=1)):
        approved = services.approve_objective(
            objective=objective, user=team.director, recent_mfa=True
        )
        services.add_progress(
            objective=approved,
            user=team.student,
            date=D(2027, 3, 3),
            progress=100,
            status=Status.ACHIEVED,
        )
    _run()  # nothing happened this week, but the result still waits for the director
    assert _recipients() == [team.director.email]


def test_former_members_deactivated_users_and_finished_theses(team: SimpleNamespace) -> None:
    _objective(team, IN_THE_WEEK)
    Membership.objects.filter(user=team.co_supervisor).update(
        active_until=dt.datetime(2027, 3, 1, tzinfo=dt.UTC)
    )
    type(team.tutor).objects.filter(pk=team.tutor.pk).update(is_active=False)
    _run()
    assert _recipients() == [team.director.email]
    other = make_team()
    _objective(other, IN_THE_WEEK)
    Thesis.objects.filter(pk=other.thesis.pk).update(status=Thesis.Status.DEFENDED)
    mail.outbox.clear()
    _run("2027-03-22T07:00:00+00:00")  # next week: nothing new anywhere
    assert mail.outbox == []


# --------------------------------------------------------------------------- once per week


def test_once_per_week_and_recipient(team: SimpleNamespace) -> None:
    _objective(team, IN_THE_WEEK)
    _run()
    _run("2027-03-15T07:30:00+00:00")  # the same Monday, again
    _run("2027-03-17T10:00:00+00:00")  # a manual run later in the week
    assert len(mail.outbox) == 3
    assert WeeklySummaryLog.objects.count() == 3
    assert set(WeeklySummaryLog.objects.values_list("week_start", flat=True)) == {D(2027, 3, 15)}


@pytest.mark.serial
def test_two_concurrent_runs_send_once(
    team: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    _objective(team, IN_THE_WEEK)
    barrier = threading.Barrier(2, timeout=5)
    real_claim = summary._claim

    def held(*args: Any, **kwargs: Any) -> Any:
        barrier.wait()
        return real_claim(*args, **kwargs)

    monkeypatch.setattr(summary, "_claim", held)
    results: list[dict[str, int]] = []

    def one_run() -> None:
        with _at(RUN):
            results.append(summary.run())

    run_threads(one_run, one_run, control=lambda: None)
    assert WeeklySummaryLog.objects.count() == 3
    assert len(mail.outbox) == 3
    assert sum(r["queued"] for r in results) == 3


def test_the_database_refuses_a_second_row_and_a_week_not_starting_on_monday(
    team: SimpleNamespace,
) -> None:
    WeeklySummaryLog.objects.create(
        thesis=team.thesis, recipient=team.director, week_start=D(2027, 3, 15)
    )
    with pytest.raises(IntegrityError), transaction.atomic():
        WeeklySummaryLog.objects.create(
            thesis=team.thesis, recipient=team.director, week_start=D(2027, 3, 15)
        )
    with pytest.raises(IntegrityError), transaction.atomic():
        WeeklySummaryLog.objects.create(
            thesis=team.thesis, recipient=team.tutor, week_start=D(2027, 3, 16)
        )


def test_a_failed_sending_is_retried_up_to_the_limit(team: SimpleNamespace) -> None:
    _objective(team, IN_THE_WEEK, level=L.CONFIDENTIAL)  # director and tutor
    with mock.patch("apps.objectives.tasks.send_mail", side_effect=OSError("down")):
        for minutes in (0, 10, 20, 30):
            _run(f"2027-03-15T07:{minutes:02d}:00+00:00")
    logs = list(WeeklySummaryLog.objects.all())
    assert [(log.status, log.attempts) for log in logs] == [
        (WeeklySummaryLog.Status.FAILED, summary.MAX_ATTEMPTS)
    ] * 2
    _run("2027-03-15T08:00:00+00:00")
    assert mail.outbox == []  # the limit is reached: no fourth attempt


# --------------------------------------------------------------------------- content


def test_the_email_is_a_link_only(team: SimpleNamespace, settings: Any) -> None:
    title = marker()
    _objective(team, IN_THE_WEEK, title=title)
    _objective(team, IN_THE_WEEK, title="Second fictif")
    _run()
    link = settings.SITE_URL + reverse("objectives:dashboard", kwargs={"thesis_id": team.thesis.pk})
    for message in mail.outbox:
        assert len(message.to) == 1 and not message.cc and not message.bcc
        assert message.subject == "Thèse CIFRE\u00a0: le point hebdomadaire des objectifs"
        assert message.body == (
            "Le point hebdomadaire des objectifs vous attend sur la plateforme\u00a0:\n"
            "\n"
            f"{link}\n"
            "\n"
            "Ce message ne contient aucun détail\u00a0: connectez-vous pour le consulter.\n"
        )
        text = message.subject + message.body
        for secret in (title, team.thesis.title, team.director.last_name, team.student.last_name):
            assert secret not in text


def test_the_task_takes_the_id_only_and_stores_no_result() -> None:
    from apps.objectives.tasks import send_weekly_summary

    assert list(inspect.signature(send_weekly_summary.run).parameters) == ["summary_id"]
    assert not getattr(send_weekly_summary, "autoretry_for", ())
    assert send_weekly_summary.ignore_result


def test_only_the_id_goes_through_the_broker(team: SimpleNamespace) -> None:
    _objective(team, IN_THE_WEEK, level=L.CONFIDENTIAL)
    with mock.patch("apps.objectives.tasks.send_weekly_summary.delay") as delay:
        _run()
    ids = sorted(str(pk) for pk in WeeklySummaryLog.objects.values_list("pk", flat=True))
    assert sorted(call.args[0] for call in delay.call_args_list) == ids
    assert all(len(call.args) == 1 and not call.kwargs for call in delay.call_args_list)


@pytest.mark.parametrize("smtp_fails", [False, True])
def test_logs_hold_no_title_address_or_body(
    team: SimpleNamespace, caplog: pytest.LogCaptureFixture, smtp_fails: bool
) -> None:
    title = marker()
    _objective(team, IN_THE_WEEK, title=title)
    caplog.clear()
    caplog.set_level(logging.DEBUG)

    def fake_send_mail(subject: str, body: str, *args: Any, **kwargs: Any) -> int:
        if smtp_fails:
            raise smtplib.SMTPException(f"server echoed {title} {team.director.email}")
        return 1

    out = io.StringIO()
    with mock.patch("apps.objectives.tasks.send_mail", side_effect=fake_send_mail), _at(RUN):
        call_command("objectives_weekly_summary", stdout=out)
    captured = " ".join(f"{r.getMessage()} {r.args}" for r in caplog.records) + out.getvalue()
    for secret in (title, team.director.email, team.thesis.title, "point hebdomadaire"):
        assert secret not in captured
    assert out.getvalue().strip() == "1 thesis(es), 3 summary e-mail(s) queued"


# --------------------------------------------------------------------------- EMAIL_TIMEOUT

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
    team: SimpleNamespace, settings: Any, silent_smtp: int
) -> None:
    settings.EMAIL_BACKEND = "django.core.mail.backends.smtp.EmailBackend"
    settings.EMAIL_HOST = "127.0.0.1"
    settings.EMAIL_PORT = silent_smtp
    settings.EMAIL_USE_TLS = False
    settings.EMAIL_TIMEOUT = 1  # 10 in base and prod (test_settings)
    _objective(team, IN_THE_WEEK, level=L.CONFIDENTIAL)
    Membership.objects.filter(user=team.tutor).update(active_until=timezone.now())  # one e-mail
    started = time.monotonic()
    with time_machine.travel(dt.datetime.fromisoformat(RUN), tick=True):
        summary.run()
    assert time.monotonic() - started < 10  # gave up after EMAIL_TIMEOUT, not SILENCE
    log = WeeklySummaryLog.objects.get()
    assert (log.status, log.attempts, log.sent_at) == (WeeklySummaryLog.Status.FAILED, 1, None)
    assert not connection.in_atomic_block
