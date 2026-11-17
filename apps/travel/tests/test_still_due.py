"""Send-time check of the travel e-mails, aligned on the other modules (branch
notifications-still-due): the shared delivery (apps.core.delivery), the same
contract (concerned(row), still_due(row)), a finished thesis skipped, every skip
logged with the row id and a fixed code only.

The state changes of each kind are covered in test_reminders.py; this file adds
what M7 lacked.
"""

from __future__ import annotations

import datetime as dt
import logging
import uuid
from types import SimpleNamespace
from typing import Any

import pytest
from django.core import mail
from django.utils import timezone

from apps.accounts.models import Membership, Thesis
from apps.travel import notifications, services, tasks
from apps.travel.models import Mission, TravelNotification
from apps.travel.tests.helpers import NOW, make_team, marker, mission_of

pytestmark = pytest.mark.django_db(transaction=True)

LOGGER = "apps.core.delivery"
UTC = dt.UTC


def _information_at_once(team: Any) -> None:
    """M11b: these e-mails are « Informations », whose default is a digest; this
    test checks their immediate sending, so the preference is set explicitly
    (preparation only, docs/modules/M11-activite.md, criterion 8)."""
    from apps.accounts.models import Membership
    from apps.accounts.permissions import roles_of
    from apps.activity.choices import Category, Frequency
    from apps.activity.preferences import default_frequency, set_preference

    for membership in Membership.objects.active().filter(thesis=team.thesis):
        user = membership.user
        if default_frequency(roles_of(user, team.thesis)) is not None:
            set_preference(user, team.thesis, Category.INFO, Frequency.IMMEDIATE)


@pytest.fixture(autouse=True)
def _now(time_machine: Any) -> None:
    time_machine.move_to(NOW, tick=True)


@pytest.fixture
def team() -> SimpleNamespace:
    return make_team()


@pytest.fixture
def log(caplog: pytest.LogCaptureFixture) -> pytest.LogCaptureFixture:
    caplog.set_level(logging.INFO, logger=LOGGER)
    return caplog


def _row(
    team: SimpleNamespace,
    kind: str,
    user: Any,
    mission: Mission | None,
    target: uuid.UUID | None = None,
    window: str = "",
) -> TravelNotification:
    # The services may have recorded (and sent) this very event: start afresh.
    target = target or uuid.uuid4()
    TravelNotification.objects.filter(
        kind=kind, target_id=target, window=window, recipient=user
    ).delete()
    return TravelNotification.objects.create(
        thesis=team.thesis, kind=kind, target_id=target, window=window, recipient=user,
        mission=mission,
    )  # fmt: skip


def _send(row: TravelNotification) -> str:
    mail.outbox.clear()
    tasks.send_travel_notification(str(row.pk))
    row.refresh_from_db()
    return str(row.status)


def _codes(log: Any) -> list[str]:
    return [r.getMessage() for r in log.records if r.name == LOGGER]


def _skipped(row: TravelNotification, code: str, log: Any) -> None:
    log.clear()
    assert _send(row) == "SKIPPED", row.kind
    assert mail.outbox == []
    assert _codes(log) == [f"travel notification skipped ({row.pk}, {code})"]


def _one_of_each(team: SimpleNamespace, time_machine: Any) -> list[TravelNotification]:
    """One row of each kind, each still due to a concerned recipient."""
    planned = mission_of(team)
    requested = mission_of(team, start_date=dt.date(2027, 3, 8), end_date=dt.date(2027, 3, 9))
    order = services.request_order(mission=requested, user=team.student, estimated_total="1")
    decided_mission = mission_of(team, start_date=dt.date(2027, 4, 5), end_date=dt.date(2027, 4, 6))
    decided = services.request_order(
        mission=decided_mission, user=team.student, estimated_total="1"
    )
    services.decide_order(order=decided, user=team.tutor, decision="GRANTED", recent_mfa=True)
    done: Mission = mission_of(
        team, payer="OTHER", start_date=dt.date(2027, 1, 18), end_date=dt.date(2027, 1, 19)
    )
    services.mark_done(mission=done, user=team.student)
    services.add_expense(mission=done, user=team.student, category="TRANSPORT", actual="80")
    return [
        _row(team, "ORDER_TO_REQUEST", team.student, planned, window="DUE"),
        _row(team, "ORDER_REQUESTED", team.tutor, requested, target=order.pk),
        _row(team, "ORDER_PENDING", team.tutor, requested, window="J7"),
        _row(team, "ORDER_DECIDED", team.student, decided_mission, target=decided.pk),
        _row(team, "RECEIPTS_MISSING", team.student, done, window="AFTER_GRACE"),
        _row(team, "RULES_CHANGED", team.student, None),
    ]


def test_every_kind_has_a_case_here() -> None:
    from apps.travel.choices import NotificationKind

    assert set(NotificationKind.values) == {
        "ORDER_TO_REQUEST",
        "ORDER_REQUESTED",
        "ORDER_PENDING",
        "ORDER_DECIDED",
        "RECEIPTS_MISSING",
        "RULES_CHANGED",
    }


def test_each_kind_still_due_is_sent(team: SimpleNamespace, time_machine: Any) -> None:
    _information_at_once(team)
    for row in _one_of_each(team, time_machine):
        assert _send(row) == "SENT", row.kind
        assert [m.to for m in mail.outbox] == [[row.recipient.email]]


@pytest.mark.parametrize("status", [Thesis.Status.DEFENDED, Thesis.Status.CLOSED])
def test_a_finished_thesis_skips_every_kind(
    team: SimpleNamespace, status: str, log: Any, time_machine: Any
) -> None:
    rows = _one_of_each(team, time_machine)
    Thesis.objects.filter(pk=team.thesis.pk).update(status=status)
    for row in rows:
        _skipped(row, "thesis_closed", log)


def test_an_ended_membership_is_logged_as_not_concerned(team: SimpleNamespace, log: Any) -> None:
    row = _row(team, "ORDER_TO_REQUEST", team.student, mission_of(team), window="DUE")
    Membership.objects.filter(user=team.student, thesis=team.thesis).update(
        active_until=timezone.now()
    )
    _skipped(row, "not_concerned", log)


def test_a_cancelled_mission_is_logged_as_not_due(team: SimpleNamespace, log: Any) -> None:
    mission = mission_of(team)
    row = _row(team, "ORDER_TO_REQUEST", team.student, mission, window="DUE")
    services.cancel_mission(mission=mission, user=team.student)
    _skipped(row, "not_due", log)


def test_a_skipped_row_queued_again_logs_once(team: SimpleNamespace, log: Any) -> None:
    mission = mission_of(team)
    row = _row(team, "ORDER_TO_REQUEST", team.student, mission, window="DUE")
    services.cancel_mission(mission=mission, user=team.student)
    _skipped(row, "not_due", log)
    assert _send(row) == "SKIPPED"
    assert len(_codes(log)) == 1


def test_the_log_holds_no_purpose_or_address(team: SimpleNamespace, log: Any) -> None:
    purpose = marker()
    mission = mission_of(team, purpose=purpose)
    row = _row(team, "ORDER_TO_REQUEST", team.student, mission, window="DUE")
    services.cancel_mission(mission=mission, user=team.student)
    log.set_level(logging.DEBUG)
    _send(row)
    for leak in (purpose, team.student.email, str(mission.pk)):
        assert leak not in log.text


def test_an_unknown_kind_is_never_concerned(team: SimpleNamespace) -> None:
    row = _row(team, "RULES_CHANGED", team.student, None)
    row.kind = "UNKNOWN"  # in memory only: the database refuses it (CHECK)
    assert notifications.concerned(row) is False
    assert notifications.still_due(row) is False
