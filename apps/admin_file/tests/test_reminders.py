"""Reminders J-14, J-3 and J+1 (overdue): windows in DATES in Paris, recipients by
item state, one e-mail per reminder whatever the number of runs (two concurrent
runs included), a link only in the e-mail, ids only through the broker, no label
or code in any log.

transaction=True: reminders are queued with transaction.on_commit, as in
production (an enclosing test transaction would never commit).
"""

from __future__ import annotations

import datetime as dt
import inspect
import io
import logging
import smtplib
import threading
from typing import Any
from unittest import mock

import pytest
import time_machine
from django.core import mail
from django.core.management import call_command
from django.urls import reverse

from apps.accounts.models import Membership, Thesis
from apps.accounts.roles import Role
from apps.accounts.tests.factories import MembershipFactory
from apps.admin_file import reminders, services
from apps.admin_file.models import ItemStatus, ReminderLog, RequiredItem, Section
from apps.admin_file.reminders import MAX_ATTEMPTS, reminder_kind
from apps.admin_file.tests.helpers import item_of, make_team, put
from apps.vault.tests.concurrency import run_threads

D, T = Role.DIRECTOR, Role.INDUSTRIAL_TUTOR
S = ItemStatus


def _at(text: str) -> time_machine.travel:
    """Freeze at an instant written with its offset (UTC or Paris)."""
    return time_machine.travel(dt.datetime.fromisoformat(text), tick=False)


def _run(text: str) -> dict[str, int]:
    with _at(text):
        return reminders.run()


def _sent_to() -> list[str]:
    return sorted(address for m in mail.outbox for address in m.to)


# --------------------------------------------------------------------------- windows (pure)


def _expected_kind(offset: int) -> str | None:
    """Written from the plan: offset = today - due date, in days."""
    if offset >= 1:
        return "OVERDUE"
    if -3 <= offset <= 0:
        return "J3"
    if -14 <= offset <= -4:
        return "J14"
    return None


@pytest.mark.parametrize("offset", range(-20, 8))
def test_windows_are_whole_days(offset: int) -> None:
    due = dt.date(2027, 3, 31)
    kind = reminder_kind(due + dt.timedelta(days=offset), due)
    assert (kind.value if kind else None) == _expected_kind(offset)


def test_reminder_kind_refuses_datetimes() -> None:
    """Windows are computed on dates only: a datetime is a programming error."""
    with pytest.raises(TypeError):
        reminder_kind(dt.datetime(2027, 3, 1, 12, 0), dt.date(2027, 3, 31))


# --------------------------------------------------------------------------- recipients


@pytest.mark.django_db(transaction=True)
def test_todo_and_rejected_remind_the_student() -> None:
    team = make_team()
    item_of(team, due_date=dt.date(2026, 12, 10))
    rejected = item_of(team, due_date=dt.date(2026, 12, 10))
    put(rejected, S.REJECTED, team.student)
    _run("2026-12-01T09:00:00+00:00")  # J-9: J14 window
    assert _sent_to() == [team.student.email, team.student.email]


@pytest.mark.django_db(transaction=True)
def test_submitted_reminds_the_validators_who_have_not_approved() -> None:
    team = make_team()
    item = item_of(
        team, section=Section.CIFRE, validator_roles=[D, T], due_date=dt.date(2026, 12, 10)
    )
    services.submit(item=item, user=team.student)
    services.approve(item=item, user=team.director)
    _run("2026-12-01T09:00:00+00:00")
    assert _sent_to() == [team.tutor.email]


@pytest.mark.django_db(transaction=True)
def test_the_submitter_is_never_reminded_as_validator() -> None:
    team = make_team()
    MembershipFactory.create(thesis=team.thesis, role=D, user=team.student)
    item = item_of(team, validator_roles=[D], due_date=dt.date(2026, 12, 10))
    services.submit(item=item, user=team.student)
    _run("2026-12-01T09:00:00+00:00")
    assert _sent_to() == [team.director.email]


@pytest.mark.django_db(transaction=True)
def test_nothing_for_validated_undated_or_frozen() -> None:
    team = make_team()
    put(item_of(team, due_date=dt.date(2026, 12, 10)), S.VALIDATED, team.student)
    item_of(team, due_date=None, mandatory=False)  # a mandatory one always has a date
    closed = make_team(status=Thesis.Status.CLOSED)
    item_of(closed, due_date=dt.date(2026, 12, 10))
    assert _run("2026-12-01T09:00:00+00:00")["queued"] == 0
    assert mail.outbox == []


@pytest.mark.django_db(transaction=True)
def test_former_and_deactivated_members_are_not_reminded() -> None:
    team = make_team()
    item_of(team, due_date=dt.date(2026, 12, 10))
    with _at("2026-11-20T09:00:00+00:00"):
        Membership.objects.filter(user=team.student).update(
            active_until=dt.datetime(2026, 11, 20, tzinfo=dt.UTC)
        )
    _run("2026-12-01T09:00:00+00:00")
    assert mail.outbox == []
    other = make_team()
    item_of(other, due_date=dt.date(2026, 12, 10))
    type(other.student).objects.filter(pk=other.student.pk).update(is_active=False)
    _run("2026-12-01T09:00:00+00:00")
    assert mail.outbox == []


# --------------------------------------------------------------------------- idempotence


@pytest.mark.django_db(transaction=True)
def test_one_email_per_window_whatever_the_number_of_runs() -> None:
    team = make_team()
    item_of(team, due_date=dt.date(2026, 12, 15))
    for day in ("2026-12-01", "2026-12-01", "2026-12-02", "2026-12-11"):  # J-14 window
        _run(f"{day}T09:00:00+00:00")
    assert len(mail.outbox) == 1
    for day in ("2026-12-12", "2026-12-15"):  # J-3 window, due day included
        _run(f"{day}T09:00:00+00:00")
    assert len(mail.outbox) == 2
    for day in ("2026-12-16", "2026-12-20"):  # overdue: once
        _run(f"{day}T09:00:00+00:00")
    assert len(mail.outbox) == 3
    kinds = list(ReminderLog.objects.order_by("created_at").values_list("kind", flat=True))
    assert kinds == ["J14", "J3", "OVERDUE"]


@pytest.mark.django_db(transaction=True)
def test_a_missed_window_is_not_sent_late() -> None:
    """First run inside J-3: no J-14 reminder afterwards."""
    team = make_team()
    item_of(team, due_date=dt.date(2026, 12, 15))
    _run("2026-12-13T09:00:00+00:00")
    assert list(ReminderLog.objects.values_list("kind", flat=True)) == ["J3"]


@pytest.mark.django_db(transaction=True)
def test_a_new_due_date_gets_new_reminders() -> None:
    team = make_team()
    item = item_of(team, due_date=dt.date(2026, 12, 15))
    _run("2026-12-01T09:00:00+00:00")
    # Revised 2026-11-14: a validator changes a catalogue due date (the student is
    # then notified too: count the reminders, not every e-mail).
    services.update_due_date(item=item, user=team.director, due_date=dt.date(2026, 12, 20))
    _run("2026-12-06T09:00:00+00:00")
    assert ReminderLog.objects.count() == 2
    assert {r.due_date for r in ReminderLog.objects.all()} == {
        dt.date(2026, 12, 15),
        dt.date(2026, 12, 20),
    }


@pytest.mark.django_db(transaction=True)
def test_failed_sending_is_retried_up_to_the_limit() -> None:
    team = make_team()
    item_of(team, due_date=dt.date(2026, 12, 15))
    with mock.patch("apps.admin_file.tasks.send_mail", side_effect=smtplib.SMTPException("no")):
        for day in range(1, MAX_ATTEMPTS + 3):
            _run(f"2026-12-{day:02d}T09:00:00+00:00")
    log = ReminderLog.objects.get()
    assert (log.status, log.attempts) == (ReminderLog.Status.FAILED, MAX_ATTEMPTS)
    _run("2026-12-10T09:00:00+00:00")
    assert mail.outbox == []  # given up: the limit is reached
    assert ReminderLog.objects.get().attempts == MAX_ATTEMPTS


@pytest.mark.django_db(transaction=True)
def test_a_failure_then_a_success() -> None:
    team = make_team()
    item_of(team, due_date=dt.date(2026, 12, 15))
    with mock.patch("apps.admin_file.tasks.send_mail", side_effect=OSError("down")):
        _run("2026-12-01T09:00:00+00:00")
    _run("2026-12-02T09:00:00+00:00")
    log = ReminderLog.objects.get()
    assert (log.status, log.attempts, len(mail.outbox)) == (ReminderLog.Status.SENT, 2, 1)


# --------------------------------------------------------------------------- e-mail content


@pytest.mark.django_db(transaction=True)
def test_the_email_is_a_link_only() -> None:
    team = make_team()
    custom = item_of(
        team,
        code="custom-0123456789ab",
        label="Renouvellement fictif",
        due_date=dt.date(2026, 12, 10),
    )
    _run("2026-12-01T09:00:00+00:00")
    [message] = mail.outbox
    assert message.to == [team.student.email]
    link = reverse("admin_file:file", kwargs={"thesis_id": team.thesis.pk})
    assert link in message.body
    text = str(message.subject) + str(message.body)
    for secret in (
        custom.label,
        custom.code,
        team.thesis.title,
        team.student.first_name,
        team.student.last_name,
        "2026-12-10",
        "10/12/2026",
    ):
        assert secret not in text, secret


@pytest.mark.django_db(transaction=True)
def test_one_message_per_recipient() -> None:
    team = make_team()
    item = item_of(
        team, section=Section.CIFRE, validator_roles=[D, T], due_date=dt.date(2026, 12, 10)
    )
    services.submit(item=item, user=team.student)
    _run("2026-12-01T09:00:00+00:00")
    assert sorted(len(m.to) for m in mail.outbox) == [1, 1]
    assert all(not m.cc and not m.bcc for m in mail.outbox)


# --------------------------------------------------------------------------- broker and logs


def test_the_task_takes_the_id_only_and_stores_no_result() -> None:
    from apps.admin_file.tasks import send_reminder_email

    assert list(inspect.signature(send_reminder_email.run).parameters) == ["reminder_id"]
    assert not getattr(send_reminder_email, "autoretry_for", ())
    assert send_reminder_email.ignore_result


@pytest.mark.django_db(transaction=True)
def test_only_the_reminder_id_goes_through_the_broker() -> None:
    team = make_team()
    item_of(team, due_date=dt.date(2026, 12, 10))
    with mock.patch("apps.admin_file.tasks.send_reminder_email.delay") as delay:
        _run("2026-12-01T09:00:00+00:00")
    log = ReminderLog.objects.get()
    delay.assert_called_once_with(str(log.pk))


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("smtp_fails", [False, True])
def test_logs_hold_no_label_code_or_body(
    caplog: pytest.LogCaptureFixture, smtp_fails: bool
) -> None:
    team = make_team()
    custom = item_of(
        team,
        code="custom-0123456789ab",
        label="Renouvellement fictif",
        due_date=dt.date(2026, 12, 10),
    )
    # Captured from here on: the factories' own debug logs (setup) are not the app's.
    caplog.clear()
    caplog.set_level(logging.DEBUG)  # every logger, every level, Celery included
    sent: list[tuple[str, str]] = []

    def fake_send_mail(subject: str, body: str, *args: Any, **kwargs: Any) -> int:
        sent.append((subject, body))
        if smtp_fails:
            raise smtplib.SMTPException("server echoed Renouvellement fictif")
        return 1

    out = io.StringIO()
    with (
        mock.patch("apps.admin_file.tasks.send_mail", side_effect=fake_send_mail),
        _at("2026-12-01T09:00:00+00:00"),
    ):
        call_command("admin_file_reminders", stdout=out)
    assert len(sent) == 1
    subject, body = sent[0]
    logged = caplog.text + "".join(str(r.args) for r in caplog.records) + out.getvalue()
    assert caplog.records  # the capture works: Celery's task trace is there
    for secret in (custom.label, custom.code, team.thesis.title, subject, body.strip()):
        assert secret not in logged, secret
    assert team.student.email not in logged


# --------------------------------------------------------------------------- boundaries


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize(
    ("instant", "due", "expected"),
    [
        # 23:30 UTC is already the next day in Paris.
        ("2026-11-16T22:59:00+00:00", "2026-12-01", None),  # 23:59 Paris, J-15
        ("2026-11-16T23:30:00+00:00", "2026-12-01", "J14"),  # 00:30 Paris, J-14
        ("2026-11-27T23:30:00+00:00", "2026-12-01", "J3"),  # 00:30 Paris, J-3
        ("2026-12-01T22:59:00+00:00", "2026-12-01", "J3"),  # 23:59 Paris, due day
        ("2026-12-01T23:30:00+00:00", "2026-12-01", "OVERDUE"),  # 00:30 Paris, J+1
        # Summer time starts on 2027-03-28 (02:00 -> 03:00 Paris).
        ("2027-03-27T22:59:00+00:00", "2027-04-11", None),  # 23:59 Paris (UTC+1), J-15
        ("2027-03-27T23:30:00+00:00", "2027-04-11", "J14"),  # 00:30 Paris, J-14
        ("2027-03-28T01:30:00+00:00", "2027-03-31", "J3"),  # 03:30 Paris (UTC+2), J-3
        ("2027-03-30T22:30:00+00:00", "2027-03-31", "J3"),  # 00:30 Paris (UTC+2), due day
        ("2027-03-31T22:30:00+00:00", "2027-03-31", "OVERDUE"),  # 00:30 Paris, J+1
        # Winter time starts on 2027-10-31 (03:00 -> 02:00 Paris).
        ("2027-10-30T21:59:00+00:00", "2027-10-30", "J3"),  # 23:59 Paris (UTC+2), due day
        ("2027-10-30T22:30:00+00:00", "2027-10-30", "OVERDUE"),  # 00:30 Paris, J+1
        ("2027-10-31T00:30:00+00:00", "2027-11-03", "J3"),  # 02:30 Paris (1st), J-3
        ("2027-10-31T01:30:00+00:00", "2027-11-03", "J3"),  # 02:30 Paris (2nd), J-3
        ("2027-10-31T22:59:00+00:00", "2027-11-14", "J14"),  # 23:59 Paris (UTC+1), J-14
        ("2027-10-31T23:00:00+00:00", "2027-11-14", "J14"),  # 00:00 Paris, J-13
        ("2027-11-03T23:00:00+00:00", "2027-11-03", "OVERDUE"),  # 00:00 Paris, J+1
    ],
)
def test_windows_follow_the_paris_date(instant: str, due: str, expected: str | None) -> None:
    team = make_team()
    item_of(team, due_date=dt.date.fromisoformat(due))
    _run(instant)
    assert list(ReminderLog.objects.values_list("kind", flat=True)) == (
        [expected] if expected else []
    )


@pytest.mark.django_db(transaction=True)
def test_no_second_reminder_across_the_time_changes() -> None:
    """Runs every 30 minutes across both night changes: each window sends once."""
    team = make_team()
    item_of(team, due_date=dt.date(2027, 4, 11))  # J-14 on 2027-03-28
    item_of(team, due_date=dt.date(2027, 10, 31))  # due on the winter-time night
    for start in ("2027-03-27T20:00:00+00:00", "2027-10-30T20:00:00+00:00"):
        t0 = dt.datetime.fromisoformat(start)
        for half_hours in range(0, 16):
            with time_machine.travel(t0 + dt.timedelta(minutes=30 * half_hours), tick=False):
                reminders.run()
    kinds = sorted(ReminderLog.objects.values_list("kind", flat=True))
    assert kinds == ["J14", "J3", "OVERDUE"]
    assert len(mail.outbox) == 3


# --------------------------------------------------------------------------- concurrency


@pytest.mark.django_db(transaction=True)
def test_two_concurrent_runs_send_each_reminder_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """The timer and a manual run at the same moment: every claim is attempted by
    both runs at once (barrier); the unique constraint of ReminderLog lets one win."""
    team = make_team()
    item_of(team, due_date=dt.date(2026, 12, 10))
    two_party = item_of(
        team, section=Section.CIFRE, validator_roles=[D, T], due_date=dt.date(2026, 12, 10)
    )
    services.submit(item=two_party, user=team.student)
    barrier = threading.Barrier(2, timeout=5)
    real_claim = reminders._claim

    def held(*args: Any, **kwargs: Any) -> Any:
        barrier.wait()
        return real_claim(*args, **kwargs)

    monkeypatch.setattr(reminders, "_claim", held)
    results: list[dict[str, int]] = []

    def one_run() -> None:
        with _at("2026-12-01T09:00:00+00:00"):
            results.append(reminders.run())

    run_threads(one_run, one_run, control=lambda: None)
    assert ReminderLog.objects.count() == 3  # student, director, tutor
    assert len(mail.outbox) == 3
    assert sorted(_sent_to()) == sorted([team.student.email, team.director.email, team.tutor.email])
    assert sum(r["queued"] for r in results) == 3


# --------------------------------------------------------------------------- command


@pytest.mark.django_db(transaction=True)
def test_the_command_prints_counts_only() -> None:
    team = make_team()
    item_of(team, due_date=dt.date(2026, 12, 10))
    out = io.StringIO()
    with _at("2026-12-01T09:00:00+00:00"):
        call_command("admin_file_reminders", stdout=out)
    assert "1" in out.getvalue()
    assert team.student.email not in out.getvalue()
    assert RequiredItem.objects.get(thesis=team.thesis).code not in out.getvalue()
