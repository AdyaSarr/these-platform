"""Reminders and event notifications of the travel module (M7, step 6): windows in Paris
DATES, once each per (event, window, recipient), a link only (CLAUDE.md rule 7), the
recipient checked again when the e-mail is sent, never a co-supervisor."""

from __future__ import annotations

import datetime as dt
import re
import smtplib
from io import StringIO
from types import SimpleNamespace
from typing import Any

import pytest
from django.conf import settings
from django.core import mail
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command

from apps.accounts.models import Membership
from apps.accounts.roles import Role
from apps.accounts.tests.factories import MembershipFactory
from apps.travel import reminders, services, tasks
from apps.travel.models import Mission, TravelNotification
from apps.travel.tests.helpers import NOW, D, make_team, marker, mission_of

pytestmark = pytest.mark.django_db(transaction=True)

UTC = dt.UTC


@pytest.fixture(autouse=True)
def _now(time_machine: Any) -> None:
    time_machine.move_to(NOW, tick=True)


@pytest.fixture
def team() -> SimpleNamespace:
    return make_team()


def _at(year: int, month: int, day: int, hour: int = 6, minute: int = 0) -> dt.datetime:
    return dt.datetime(year, month, day, hour, minute, tzinfo=UTC)


def _rows(kind: str | None = None) -> list[TravelNotification]:
    rows = TravelNotification.objects.order_by("created_at", "pk")
    return list(rows.filter(kind=kind) if kind else rows)


def _to(user: Any) -> list[Any]:
    return [m for m in mail.outbox if m.to == [user.email]]


def _pdf() -> SimpleUploadedFile:
    return SimpleUploadedFile("f.pdf", b"%PDF-1.7\n%fictif\n%%EOF\n", "application/pdf")


# =========================================================================== windows


@pytest.mark.parametrize(
    ("days", "window"),
    [(23, None), (22, "EARLY"), (16, "EARLY"), (15, "DUE"), (0, "DUE"), (-1, None)],
)
def test_order_windows(days: int, window: str | None) -> None:
    assert reminders.order_window(days, lead_days=15) == window


@pytest.mark.parametrize(
    ("days", "window"), [(8, None), (7, "J7"), (2, "J7"), (1, "J1"), (0, "J1"), (-1, None)]
)
def test_pending_windows(days: int, window: str | None) -> None:
    assert reminders.pending_window(days) == window


@pytest.mark.parametrize(
    ("days", "window"),
    [(2, None), (3, "AFTER_GRACE"), (13, "AFTER_GRACE"), (14, "AFTER_14"), (60, "AFTER_14")],
)
def test_receipt_windows(days: int, window: str | None) -> None:
    assert reminders.receipt_window(days, grace_days=3) == window


# =========================================================================== to request


def test_an_approval_to_request_is_reminded_twice_once_each(
    team: SimpleNamespace, time_machine: Any
) -> None:
    secret = marker()
    mission = mission_of(team, purpose=secret)  # ST, departure 2027-02-08
    reminders.run(_at(2027, 1, 20))  # 19 days: EARLY (N = 15)
    reminders.run(_at(2027, 1, 20, 18))  # same day again: nothing new
    assert [r.window for r in _rows("ORDER_TO_REQUEST")] == ["EARLY"]
    reminders.run(_at(2027, 1, 24))  # 15 days: DUE
    reminders.run(_at(2027, 1, 30))
    assert [r.window for r in _rows("ORDER_TO_REQUEST")] == ["EARLY", "DUE"]
    sent = _to(team.student)
    assert len(sent) == 2
    for message in sent:
        assert secret not in message.subject + message.body
        urls = re.findall(r"https?://\S+", message.body)
        assert urls == [f"{settings.SITE_URL}/t/{team.thesis.pk}/travel/missions/{mission.pk}/"]
    services.request_order(mission=mission, user=team.student, estimated_total="10")
    reminders.run(_at(2027, 2, 1))
    assert len(_rows("ORDER_TO_REQUEST")) == 2


def test_the_lead_days_come_from_the_policy(team: SimpleNamespace) -> None:
    services.set_reminders(
        thesis=team.thesis,
        user=team.student,
        recent_mfa=True,
        order_lead_days=30,
        receipt_grace_days=3,
    )
    mission_of(team)  # 19 days before: within N = 30
    reminders.run(_at(2027, 1, 20))
    assert [r.window for r in _rows("ORDER_TO_REQUEST")] == ["DUE"]


def test_paris_dates(team: SimpleNamespace) -> None:
    """23:30 UTC on 23 Jan is 00:30 on 24 Jan in Paris: 15 days before 8 Feb."""
    mission_of(team)
    reminders.run(_at(2027, 1, 23, 23, 30))
    assert [r.window for r in _rows("ORDER_TO_REQUEST")] == ["DUE"]


@pytest.mark.parametrize(
    "fields", [{"payer": "OTHER"}, {"start_date": D(2027, 1, 19), "end_date": D(2027, 1, 19)}]
)
def test_no_reminder_without_an_approval_to_ask(
    team: SimpleNamespace, fields: dict[str, Any]
) -> None:
    mission_of(team, **fields)
    reminders.run(_at(2027, 1, 20))
    assert _rows("ORDER_TO_REQUEST") == []


def test_a_cancelled_mission_is_not_reminded(team: SimpleNamespace) -> None:
    mission = mission_of(team)
    services.cancel_mission(mission=mission, user=team.student)
    reminders.run(_at(2027, 1, 20))
    assert _rows() == []


def test_a_refused_approval_is_reminded_again(team: SimpleNamespace) -> None:
    mission = mission_of(team)
    order = services.request_order(mission=mission, user=team.student, estimated_total="10")
    services.decide_order(
        order=order, user=team.tutor, decision="REFUSED", reason="OTHER", recent_mfa=True
    )
    reminders.run(_at(2027, 1, 24))
    assert [r.recipient_id for r in _rows("ORDER_TO_REQUEST")] == [team.student.pk]


# =========================================================================== pending


@pytest.mark.parametrize(
    ("payer", "decider", "other"), [("ST", "tutor", "director"), ("LIRMM", "director", "tutor")]
)
def test_a_pending_approval_reminds_the_payers_person(
    team: SimpleNamespace, payer: str, decider: str, other: str
) -> None:
    mission = mission_of(team, payer=payer)
    services.request_order(mission=mission, user=team.student, estimated_total="10")
    mail.outbox.clear()
    reminders.run(_at(2027, 2, 3))  # 5 days: J7
    reminders.run(_at(2027, 2, 7))  # 1 day: J1
    rows = _rows("ORDER_PENDING")
    assert [(r.window, r.recipient_id) for r in rows] == [
        ("J7", getattr(team, decider).pk),
        ("J1", getattr(team, decider).pk),
    ]
    assert _to(getattr(team, other)) == []


# =========================================================================== receipts


def _done(team: SimpleNamespace, time_machine: Any) -> Mission:
    mission: Mission = mission_of(team, payer="OTHER")
    time_machine.move_to(_at(2027, 2, 11), tick=True)
    services.mark_done(mission=mission, user=team.student)
    return mission


def test_missing_receipts_are_reminded_after_the_return(
    team: SimpleNamespace, time_machine: Any
) -> None:
    mission = _done(team, time_machine)
    services.add_expense(mission=mission, user=team.student, category="TRANSPORT", actual="80")
    reminders.run(_at(2027, 2, 12))  # 2 days after the return (10 Feb): before the grace
    assert _rows("RECEIPTS_MISSING") == []
    reminders.run(_at(2027, 2, 13))  # 3 days
    reminders.run(_at(2027, 2, 24))  # 14 days
    reminders.run(_at(2027, 3, 3))
    assert [r.window for r in _rows("RECEIPTS_MISSING")] == ["AFTER_GRACE", "AFTER_14"]


def test_complete_expenses_are_not_reminded(team: SimpleNamespace, time_machine: Any) -> None:
    mission = _done(team, time_machine)
    line = services.add_expense(
        mission=mission, user=team.student, category="TRANSPORT", actual="8"
    )
    services.attach_receipt(expense=line, user=team.student, upload=_pdf())
    services.add_expense(
        mission=mission, user=team.student, category="MEALS", actual="15", flat_rate=True
    )
    reminders.run(_at(2027, 2, 20))
    assert _rows("RECEIPTS_MISSING") == []


def test_a_flat_rate_line_without_actual_amount_is_reminded(
    team: SimpleNamespace, time_machine: Any
) -> None:
    mission = _done(team, time_machine)
    services.add_expense(mission=mission, user=team.student, category="MEALS", flat_rate=True)
    reminders.run(_at(2027, 2, 20))
    assert len(_rows("RECEIPTS_MISSING")) == 1


# =========================================================================== events


def test_a_request_notifies_the_payers_person(team: SimpleNamespace) -> None:
    services.request_order(
        mission=mission_of(team, payer="LIRMM"), user=team.student, estimated_total="1"
    )
    assert [r.recipient_id for r in _rows("ORDER_REQUESTED")] == [team.director.pk]
    assert len(_to(team.director)) == 1 and _to(team.tutor) == []


def test_a_decision_notifies_the_student(team: SimpleNamespace) -> None:
    secret = marker()
    order = services.request_order(mission=mission_of(team), user=team.student, estimated_total="1")
    services.decide_order(
        order=order,
        user=team.tutor,
        decision="REFUSED",
        reason="OTHER",
        comment=secret,
        recent_mfa=True,
    )
    assert [r.recipient_id for r in _rows("ORDER_DECIDED")] == [team.student.pk]
    (message,) = _to(team.student)
    assert secret not in message.body and "OTHER" not in message.body


@pytest.mark.parametrize(("actor", "told"), [("tutor", "director"), ("director", "tutor")])
def test_rules_and_targets_notify_the_other_supervisor_and_the_student(
    team: SimpleNamespace, actor: str, told: str
) -> None:
    services.set_rules(
        thesis=team.thesis, user=getattr(team, actor), recent_mfa=True, gap_alert_points=15
    )
    services.set_target(
        thesis=team.thesis, user=getattr(team, actor), recent_mfa=True, cifre_year=1, st_percent=60
    )
    recipients = sorted(r.recipient_id for r in _rows("RULES_CHANGED"))
    assert recipients == sorted([getattr(team, told).pk, team.student.pk] * 2)
    assert _to(getattr(team, actor)) == []


def test_unchanged_rules_notify_nobody(team: SimpleNamespace) -> None:
    services.set_rules(thesis=team.thesis, user=team.tutor, recent_mfa=True, gap_alert_points=10)
    assert _rows() == []


# =========================================================================== who, how


def test_a_co_supervisor_never_receives_anything(team: SimpleNamespace, time_machine: Any) -> None:
    mission = mission_of(team, payer="LIRMM")
    order = services.request_order(mission=mission, user=team.student, estimated_total="1")
    services.decide_order(order=order, user=team.director, decision="GRANTED", recent_mfa=True)
    services.set_rules(thesis=team.thesis, user=team.director, recent_mfa=True, gap_alert_points=20)
    mission_of(team, payer="LIRMM", start_date=D(2027, 2, 15), end_date=D(2027, 2, 16))
    for day in range(20, 32):
        reminders.run(_at(2027, 1, day))
    assert _rows() != []
    assert _to(team.co_supervisor) == []
    assert not TravelNotification.objects.filter(recipient=team.co_supervisor).exists()


def test_a_former_member_gets_nothing(team: SimpleNamespace) -> None:
    mission_of(team)
    Membership.objects.filter(thesis=team.thesis, user=team.student).update(active_until=NOW)
    reminders.run(_at(2027, 1, 20))
    assert _rows() == []


def test_the_recipient_is_checked_again_at_sending(team: SimpleNamespace) -> None:
    mission = mission_of(team)
    services.request_order(mission=mission, user=team.student, estimated_total="1")
    (row,) = _rows("ORDER_REQUESTED")
    # The tutor becomes a co-supervisor before the e-mail goes: no longer ST's person.
    Membership.objects.filter(thesis=team.thesis, user=team.tutor).update(active_until=NOW)
    MembershipFactory.create(thesis=team.thesis, user=team.tutor, role=Role.CO_SUPERVISOR)
    TravelNotification.objects.filter(pk=row.pk).update(status="PENDING", attempts=0)
    mail.outbox.clear()
    tasks.send_travel_notification(str(row.pk))
    row.refresh_from_db()
    assert row.status == "SKIPPED" and mail.outbox == []


def test_a_failed_e_mail_is_sent_again_by_the_next_run(
    team: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    mission_of(team)
    from apps.travel import tasks as travel_tasks

    def broken(*args: Any, **kwargs: Any) -> None:
        raise smtplib.SMTPException("down")

    monkeypatch.setattr(travel_tasks, "send_mail", broken)
    reminders.run(_at(2027, 1, 20))
    (row,) = _rows()
    assert row.status == "FAILED"
    monkeypatch.undo()
    result = reminders.run(_at(2027, 1, 20, 12))
    row.refresh_from_db()
    assert (row.status, result.requeued) == ("SENT", 1)


def test_a_finished_thesis_is_not_reminded(team: SimpleNamespace) -> None:
    from apps.accounts.models import Thesis

    mission_of(team)
    Thesis.objects.filter(pk=team.thesis.pk).update(status=Thesis.Status.DEFENDED)
    reminders.run(_at(2027, 1, 20))
    assert _rows() == []


def test_the_command_prints_counts_only(team: SimpleNamespace) -> None:
    mission_of(team, purpose=marker())
    out = StringIO()
    call_command("travel_reminders", stdout=out)
    text = out.getvalue()
    assert re.fullmatch(r"[0-9a-z ,()'.\n-]+", text), text
    assert "1 approval to request" in text
