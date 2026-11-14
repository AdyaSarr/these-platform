"""Item notifications (reopening, due dates): the reminders' mechanism, for events.

A FAILED notification is queued again by the daily `admin_file_reminders` run,
up to MAX_ATTEMPTS; a notification is sent once even if queued twice.
"""

from __future__ import annotations

import datetime as dt
import inspect
import io
import smtplib
from unittest import mock

import pytest
from django.core import mail
from django.core.management import call_command

from apps.accounts.roles import Role
from apps.admin_file import reminders, services, tasks
from apps.admin_file.models import ItemNotification, Section
from apps.admin_file.reminders import MAX_ATTEMPTS
from apps.admin_file.tests.helpers import item_of, make_team

pytestmark = pytest.mark.django_db(transaction=True)


def _changed_due_date() -> ItemNotification:
    team = make_team()
    item = item_of(
        team,
        section=Section.CIFRE,
        validator_roles=[Role.INDUSTRIAL_TUTOR],
        due_date=dt.date(2027, 1, 15),
    )
    with mock.patch("apps.admin_file.tasks.send_mail", side_effect=smtplib.SMTPException("no")):
        services.update_due_date(item=item, user=team.tutor, due_date=dt.date(2027, 2, 1))
    return ItemNotification.objects.get()


def test_a_failed_notification_is_retried_by_the_daily_run() -> None:
    note = _changed_due_date()
    assert (note.status, note.attempts) == ("FAILED", 1)
    reminders.run()
    note.refresh_from_db()
    assert (note.status, note.attempts) == ("SENT", 2)
    assert len(mail.outbox) == 1


def test_retries_stop_at_the_limit() -> None:
    note = _changed_due_date()
    with mock.patch("apps.admin_file.tasks.send_mail", side_effect=OSError("down")):
        for _ in range(MAX_ATTEMPTS + 2):
            reminders.run()
    note.refresh_from_db()
    assert (note.status, note.attempts) == ("FAILED", MAX_ATTEMPTS)
    reminders.run()
    assert mail.outbox == []


def test_sent_once_even_if_queued_twice() -> None:
    note = _changed_due_date()
    tasks.send_item_notification(str(note.pk))
    tasks.send_item_notification(str(note.pk))
    assert len(mail.outbox) == 1


def test_the_task_takes_the_id_only_and_stores_no_result() -> None:
    assert list(inspect.signature(tasks.send_item_notification.run).parameters) == [
        "notification_id"
    ]
    assert tasks.send_item_notification.ignore_result


def test_the_command_prints_counts_only() -> None:
    note = _changed_due_date()
    out = io.StringIO()
    call_command("admin_file_reminders", stdout=out)
    assert note.recipient.email not in out.getvalue()
    assert "notifications" in out.getvalue()
