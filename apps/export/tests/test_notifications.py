"""The alert e-mail (M13b, criterion 13): to the student, at once, whatever the
preferences, also in CLOSED; a link only (and the date of the export)."""

from __future__ import annotations

import inspect
from types import SimpleNamespace
from typing import Any

import pytest
from django.conf import settings
from django.core import mail

from apps.accounts.models import Thesis
from apps.export import notifications, services, tasks
from apps.export.models import ExportNotification
from apps.export.tests import helpers as h

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def team(time_machine: Any) -> SimpleNamespace:
    time_machine.move_to(h.NOW, tick=True)
    return h.make_team()


def test_every_send_goes_through_the_shared_delivery() -> None:
    source = inspect.getsource(tasks)
    assert "deliver_once(" in source and "send_mail" not in source
    for name in ("concerned", "still_due", "skip_reason"):
        assert callable(getattr(notifications, name, None)), name


def test_the_student_is_told_at_once_by_a_link(team: SimpleNamespace) -> None:
    from apps.activity import preferences

    for category, frequency in (("INFO", "WEEKLY"), ("ACTION", "WEEKLY"), ("DEADLINE", "DAILY")):
        preferences.set_preference(team.student, team.thesis, category, frequency)
    mail.outbox.clear()
    h.run(team)
    (message,) = mail.outbox
    assert message.to == [team.student.email]
    assert settings.SITE_URL + h.url(team, "page") in message.body
    assert "20 janvier 2027" in message.body  # the date of the export, Paris time
    assert team.thesis.title not in message.body and team.thesis.title not in message.subject
    assert ExportNotification.objects.get().status == "SENT"


def test_also_when_the_thesis_is_closed(team: SimpleNamespace) -> None:
    Thesis.objects.filter(pk=team.thesis.pk).update(status=Thesis.Status.CLOSED)
    mail.outbox.clear()
    h.run(team)
    assert [m.to for m in mail.outbox] == [[team.student.email]]


def test_an_st_agreement_is_told_to_the_student(team: SimpleNamespace) -> None:
    mail.outbox.clear()
    services.grant_st_consent(thesis=team.thesis, user=team.tutor, recent_mfa=True)
    (message,) = mail.outbox
    assert message.to == [team.student.email]
    assert settings.SITE_URL + h.url(team, "page") in message.body


def test_no_mail_to_anybody_else(team: SimpleNamespace) -> None:
    mail.outbox.clear()
    h.run(team)
    assert {tuple(m.to) for m in mail.outbox} == {(team.student.email,)}
