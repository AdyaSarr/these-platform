"""Send-time check of the administrative file's e-mails (branch notifications-still-due).

Under the row's lock, a reminder (ReminderLog) or an item notification
(ItemNotification) is sent only if the thesis is open, the recipient is still
concerned (active account and membership, the role the e-mail is for, the read
right of the item's section) and the item still calls for it; otherwise SKIPPED,
nothing sent, logged with the row id and a fixed code only.

The rows are created directly (no service): each test sets the state that the
check must find at send time.
"""

from __future__ import annotations

import datetime as dt
import logging
import threading
import uuid
from types import SimpleNamespace
from typing import Any

import pytest
from django.core import mail
from django.utils import timezone

from apps.accounts.models import Membership, Thesis
from apps.accounts.roles import Role
from apps.accounts.tests.factories import MembershipFactory
from apps.admin_file import notifications, reminders, tasks
from apps.admin_file.models import (
    DueDateProposal,
    ItemNotification,
    ItemReopening,
    ItemStatus,
    ReminderLog,
    RequiredItem,
    Section,
)
from apps.admin_file.tests.factories import ItemApprovalFactory
from apps.admin_file.tests.helpers import item_of, make_team, put

D, T = Role.DIRECTOR, Role.INDUSTRIAL_TUTOR
S = ItemStatus
N = ItemNotification.Kind
LOGGER = "apps.core.delivery"


@pytest.fixture
def team(db: None) -> SimpleNamespace:
    return make_team()


@pytest.fixture
def log(caplog: pytest.LogCaptureFixture) -> pytest.LogCaptureFixture:
    caplog.set_level(logging.INFO, logger=LOGGER)
    return caplog


# --------------------------------------------------------------------------- helpers


def _due(item: RequiredItem) -> dt.date:
    assert item.due_date is not None  # the factory always gives one
    return item.due_date


def _reminder(item: RequiredItem, recipient: Any) -> ReminderLog:
    return ReminderLog.objects.create(
        item=item, kind="J14", due_date=_due(item), recipient=recipient
    )


def _note(item: RequiredItem, kind: str, recipient: Any, event_id: Any = None) -> ItemNotification:
    return ItemNotification.objects.create(
        item=item, kind=kind, event_id=event_id or uuid.uuid4(), recipient=recipient
    )


def _send(row: ReminderLog | ItemNotification) -> str:
    if isinstance(row, ReminderLog):
        tasks.send_reminder_email(str(row.pk))
    else:
        tasks.send_item_notification(str(row.pk))
    row.refresh_from_db()
    return str(row.status)


def _sent(row: ReminderLog | ItemNotification) -> None:
    assert _send(row) == "SENT"
    assert [m.to for m in mail.outbox] == [[row.recipient.email]]


def _skipped(row: ReminderLog | ItemNotification, code: str, log: Any) -> None:
    assert _send(row) == "SKIPPED"
    assert mail.outbox == []
    label = "admin_file reminder" if isinstance(row, ReminderLog) else "admin_file notification"
    assert [r.getMessage() for r in log.records if r.name == LOGGER] == [
        f"{label} skipped ({row.pk}, {code})"
    ]


def _end(user: Any, thesis: Thesis) -> None:
    """End every membership of ``user`` on ``thesis``; the account stays active."""
    Membership.objects.filter(user=user, thesis=thesis).update(active_until=timezone.now())


def _switch(user: Any, thesis: Thesis, role: Role) -> None:
    """End the memberships of ``user`` and give them ``role`` instead."""
    _end(user, thesis)
    MembershipFactory.create(thesis=thesis, user=user, role=role)


def _submitted(team: SimpleNamespace, **kwargs: Any) -> RequiredItem:
    return put(item_of(team, **kwargs), S.SUBMITTED, team.student)


def _cifre_submitted(team: SimpleNamespace) -> RequiredItem:
    return _submitted(team, section=Section.CIFRE, validator_roles=[D, T])


def _proposal(item: RequiredItem, team: SimpleNamespace, status: str = "PENDING") -> Any:
    decided: dict[str, Any] = {}
    if status in ("ACCEPTED", "REFUSED", "SUPERSEDED"):
        decided = {"decided_by": team.director, "decided_at": timezone.now()}
    elif status == "WITHDRAWN":
        decided = {"decided_at": timezone.now()}
    return DueDateProposal.objects.create(
        item=item,
        proposed_by=team.student,
        from_date=item.due_date,
        to_date=_due(item) + dt.timedelta(days=30),
        reason_code="OTHER",
        status=status,
        created_by=team.student,
        **decided,
    )


def _reopened(team: SimpleNamespace) -> tuple[RequiredItem, ItemReopening]:
    """An item validated by the director in cycle 1, then reopened by the student."""
    item = put(item_of(team), S.VALIDATED, team.student)
    ItemApprovalFactory.create(
        item=item, cycle=1, role=D, user=team.director, submitted_by=team.student
    )
    validated_at = item.validated_at
    assert validated_at is not None
    put(item, S.TODO, team.student)
    RequiredItem.objects.filter(pk=item.pk).update(cycle=1)
    item.refresh_from_db()
    reopening = ItemReopening.objects.create(
        item=item,
        cycle=1,
        reopened_by=team.student,
        reason_code="AMENDMENT",
        validated_at=validated_at,
        created_by=team.student,
    )
    return item, reopening


def _one_of_each(team: SimpleNamespace) -> list[ReminderLog | ItemNotification]:
    """One row of each kind, each still due to a concerned recipient."""
    item = item_of(team)
    reopened, reopening = _reopened(team)
    pending = _proposal(item, team)
    decided = item_of(team)
    accepted = _proposal(decided, team, "ACCEPTED")
    return [
        _reminder(item, team.student),
        _note(reopened, N.REOPENED, team.director, reopening.pk),
        _note(item, N.DUE_PROPOSED, team.director, pending.pk),
        _note(decided, N.DUE_ACCEPTED, team.student, accepted.pk),
        _note(decided, N.DUE_REFUSED, team.student, accepted.pk),
        _note(item, N.DUE_CHANGED, team.student),
    ]


# --------------------------------------------------------------------------- every kind


def test_every_kind_has_a_case_here() -> None:
    covered = {"J14", "REOPENED", "DUE_PROPOSED", "DUE_ACCEPTED", "DUE_REFUSED", "DUE_CHANGED"}
    assert set(N.values) <= covered


def test_each_kind_still_due_is_sent(team: SimpleNamespace) -> None:
    """Positive control: a check that skipped everything would fail here."""
    rows = _one_of_each(team)
    for row in rows:
        assert _send(row) == "SENT", row.kind
    assert len(mail.outbox) == len(rows)


@pytest.mark.parametrize("status", [Thesis.Status.DEFENDED, Thesis.Status.CLOSED])
def test_a_finished_thesis_skips_every_kind(team: SimpleNamespace, status: str, log: Any) -> None:
    rows = _one_of_each(team)
    Thesis.objects.filter(pk=team.thesis.pk).update(status=status)
    for row in rows:
        assert _send(row) == "SKIPPED", row.kind
    assert mail.outbox == []
    assert {r.getMessage().rsplit(", ", 1)[1] for r in log.records if r.name == LOGGER} == {
        "thesis_closed)"
    }


def test_an_ended_membership_with_an_active_account_skips_every_kind(
    team: SimpleNamespace, log: Any
) -> None:
    """The defect of M3: a removed member whose account is still active."""
    rows = _one_of_each(team)
    _end(team.student, team.thesis)
    _end(team.director, team.thesis)
    assert team.student.is_active and team.director.is_active
    for row in rows:
        assert _send(row) == "SKIPPED", row.kind
    assert mail.outbox == []
    codes = [r.getMessage().rsplit(", ", 1)[1] for r in log.records if r.name == LOGGER]
    assert codes == ["not_concerned)"] * len(rows)


def test_a_deactivated_account_skips_every_kind(team: SimpleNamespace) -> None:
    rows = _one_of_each(team)
    type(team.student).objects.filter(pk__in=[team.student.pk, team.director.pk]).update(
        is_active=False
    )
    for row in rows:
        assert _send(row) == "SKIPPED", row.kind
    assert mail.outbox == []


def test_a_skipped_row_queued_again_sends_nothing_and_logs_once(
    team: SimpleNamespace, log: Any
) -> None:
    row = _reminder(put(item_of(team), S.VALIDATED, team.student), team.student)
    _skipped(row, "not_due", log)
    assert _send(row) == "SKIPPED"
    assert mail.outbox == []
    assert len([r for r in log.records if r.name == LOGGER]) == 1


def test_the_log_holds_no_label_code_or_address(team: SimpleNamespace, log: Any) -> None:
    item = item_of(team, code="custom-0123456789ab", label="Fictitious label")
    row = _reminder(item, team.student)
    _end(team.student, team.thesis)
    log.set_level(logging.DEBUG)
    _send(row)
    for leak in (item.label, item.code, team.student.email, str(item.pk)):
        assert leak not in log.text


# --------------------------------------------------------------------------- reminders


@pytest.mark.parametrize("status", [S.TODO, S.REJECTED])
def test_a_reminder_to_the_student_of_an_awaited_item_is_sent(
    team: SimpleNamespace, status: str
) -> None:
    item = put(item_of(team), status, team.student)
    _sent(_reminder(item, team.student))


@pytest.mark.parametrize("status", [S.SUBMITTED, S.VALIDATED])
def test_a_reminder_to_the_student_of_an_item_handed_in_meanwhile_is_skipped(
    team: SimpleNamespace, status: str, log: Any
) -> None:
    item = item_of(team)
    row = _reminder(item, team.student)
    put(item, status, team.student)
    _skipped(row, "not_due", log)


def test_a_reminder_to_a_pending_validator_is_sent(team: SimpleNamespace) -> None:
    _sent(_reminder(_cifre_submitted(team), team.tutor))


def test_a_reminder_to_a_validator_who_approved_meanwhile_is_skipped(
    team: SimpleNamespace, log: Any
) -> None:
    item = _cifre_submitted(team)
    row = _reminder(item, team.tutor)
    ItemApprovalFactory.create(
        item=item, cycle=1, role=T, user=team.tutor, submitted_by=team.student
    )
    _skipped(row, "not_due", log)


def test_a_reminder_to_a_validator_whose_role_was_approved_by_another_holder_is_skipped(
    team: SimpleNamespace, log: Any
) -> None:
    second = MembershipFactory.create(thesis=team.thesis, role=T, user__mfa=False).user
    item = _cifre_submitted(team)
    row = _reminder(item, team.tutor)
    ItemApprovalFactory.create(item=item, cycle=1, role=T, user=second, submitted_by=team.student)
    _skipped(row, "not_due", log)


def test_a_reminder_to_the_submitter_of_the_cycle_is_skipped(
    team: SimpleNamespace, log: Any
) -> None:
    """A director who handed the item in themselves is never its validator."""
    item = put(item_of(team), S.SUBMITTED, team.director)
    _skipped(_reminder(item, team.director), "not_due", log)


@pytest.mark.parametrize("status", [S.VALIDATED, S.TODO, S.REJECTED])
def test_a_reminder_to_a_validator_of_an_item_decided_or_reopened_is_skipped(
    team: SimpleNamespace, status: str, log: Any
) -> None:
    item = _cifre_submitted(team)
    row = _reminder(item, team.tutor)
    put(item, status, team.student)
    _skipped(row, "not_due", log)


def test_a_reminder_of_a_moved_due_date_is_skipped(team: SimpleNamespace, log: Any) -> None:
    item = item_of(team)
    row = _reminder(item, team.student)
    RequiredItem.objects.filter(pk=item.pk).update(due_date=_due(item) + dt.timedelta(days=7))
    _skipped(row, "not_due", log)


def test_a_reminder_of_an_erased_due_date_is_skipped(team: SimpleNamespace, log: Any) -> None:
    item = item_of(team, mandatory=False)
    row = _reminder(item, team.student)
    RequiredItem.objects.filter(pk=item.pk).update(due_date=None)
    _skipped(row, "not_due", log)


def test_a_reminder_to_a_validator_who_changed_role_is_skipped(
    team: SimpleNamespace, log: Any
) -> None:
    item = _cifre_submitted(team)
    row = _reminder(item, team.tutor)
    _switch(team.tutor, team.thesis, Role.GUEST)
    _skipped(row, "not_concerned", log)


def test_a_failed_reminder_retried_the_next_day_is_sent_if_the_item_is_still_awaited(
    team: SimpleNamespace,
) -> None:
    """Decision of the plan: no window check for the reminders of the file."""
    item = item_of(team, due_date=timezone.localdate() + dt.timedelta(days=10))
    row = _reminder(item, team.student)
    ReminderLog.objects.filter(pk=row.pk).update(status="FAILED", attempts=1)
    _sent(row)


# --------------------------------------------------------------------------- item events


def test_reopened_is_skipped_once_the_item_is_validated_again(
    team: SimpleNamespace, log: Any
) -> None:
    item, reopening = _reopened(team)
    row = _note(item, N.REOPENED, team.director, reopening.pk)
    put(item, S.VALIDATED, team.student)
    _skipped(row, "not_due", log)


def test_reopened_is_skipped_for_a_former_holder_of_the_approval_role(
    team: SimpleNamespace, log: Any
) -> None:
    item, reopening = _reopened(team)
    row = _note(item, N.REOPENED, team.director, reopening.pk)
    _switch(team.director, team.thesis, Role.CO_SUPERVISOR)
    _skipped(row, "not_concerned", log)


@pytest.mark.parametrize("status", ["ACCEPTED", "REFUSED", "WITHDRAWN", "SUPERSEDED"])
def test_due_proposed_is_skipped_once_the_proposal_is_no_longer_pending(
    team: SimpleNamespace, status: str, log: Any
) -> None:
    item = item_of(team)
    row = _note(item, N.DUE_PROPOSED, team.director, _proposal(item, team, status).pk)
    _skipped(row, "not_due", log)


def test_due_proposed_is_skipped_for_a_former_validator(team: SimpleNamespace, log: Any) -> None:
    item = item_of(team)
    row = _note(item, N.DUE_PROPOSED, team.director, _proposal(item, team).pk)
    _switch(team.director, team.thesis, Role.CO_SUPERVISOR)
    _skipped(row, "not_concerned", log)


def test_due_proposed_is_skipped_for_its_author(team: SimpleNamespace, log: Any) -> None:
    MembershipFactory.create(thesis=team.thesis, role=D, user=team.student)
    item = item_of(team)
    row = _note(item, N.DUE_PROPOSED, team.student, _proposal(item, team).pk)
    _skipped(row, "not_concerned", log)


@pytest.mark.parametrize("kind", [N.DUE_ACCEPTED, N.DUE_REFUSED])
def test_a_decision_is_skipped_for_someone_who_did_not_propose(
    team: SimpleNamespace, kind: str, log: Any
) -> None:
    item = item_of(team)
    row = _note(item, kind, team.co_supervisor, _proposal(item, team, "ACCEPTED").pk)
    _skipped(row, "not_concerned", log)


@pytest.mark.parametrize("kind", [N.DUE_ACCEPTED, N.DUE_REFUSED, N.DUE_CHANGED])
def test_a_student_event_is_skipped_for_a_former_student(
    team: SimpleNamespace, kind: str, log: Any
) -> None:
    item = item_of(team)
    row = _note(item, kind, team.student, _proposal(item, team, "ACCEPTED").pk)
    _switch(team.student, team.thesis, Role.GUEST)
    _skipped(row, "not_concerned", log)


def test_due_changed_stays_due_after_another_change(team: SimpleNamespace) -> None:
    """An accomplished event stays due: each change sends its own e-mail."""
    item = item_of(team)
    row = _note(item, N.DUE_CHANGED, team.student)
    RequiredItem.objects.filter(pk=item.pk).update(due_date=_due(item) + dt.timedelta(days=3))
    _sent(row)


def test_an_unknown_kind_is_never_concerned(team: SimpleNamespace) -> None:
    row = _note(item_of(team), N.DUE_CHANGED, team.student)
    row.kind = "UNKNOWN"  # in memory only: the database refuses it (CHECK)
    assert notifications.concerned(row) is False
    assert notifications.still_due(row) is False


# --------------------------------------------------------------------------- rows left PENDING


@pytest.mark.django_db(transaction=True)
def test_a_reminder_left_pending_by_the_old_code_stays_inert_until_its_task_runs() -> None:
    """Before this branch, a deactivated recipient left the row PENDING. The daily
    run never queues a PENDING row again (only FAILED ones): the row stays PENDING,
    nothing is sent; if a copy of its task ever runs, the row becomes SKIPPED."""
    team = make_team()
    item = item_of(team, due_date=timezone.localdate() + dt.timedelta(days=10))
    row = _reminder(item, team.student)  # J14 of today, as the old run had left it
    note = _note(item, N.DUE_CHANGED, team.student)
    type(team.student).objects.filter(pk=team.student.pk).update(is_active=False)
    reminders.run()
    row.refresh_from_db()
    note.refresh_from_db()
    assert (row.status, row.attempts, note.status, note.attempts) == ("PENDING", 0, "PENDING", 0)
    assert mail.outbox == []
    assert (_send(row), _send(note)) == ("SKIPPED", "SKIPPED")
    assert mail.outbox == []


@pytest.mark.django_db(transaction=True)
def test_a_reactivated_account_gets_no_reminder_of_a_window_left_pending() -> None:
    """The same row PENDING, the account reactivated within the same window: the
    daily run finds the row (unique constraint) and queues nothing, so that
    window's reminder is never sent; the next window is a new row, sent."""
    team = make_team()
    today = timezone.localdate()
    item = item_of(team, due_date=today + dt.timedelta(days=10))
    row = _reminder(item, team.student)  # J14, left PENDING by the old code
    type(team.student).objects.filter(pk=team.student.pk).update(is_active=False)
    type(team.student).objects.filter(pk=team.student.pk).update(is_active=True)
    assert reminders.run(today)["queued"] == 0  # still the J14 window
    row.refresh_from_db()
    assert (row.status, row.attempts) == ("PENDING", 0)
    assert mail.outbox == []
    assert reminders.run(today + dt.timedelta(days=7))["queued"] == 1  # J3: a new row
    assert [m.to for m in mail.outbox] == [[team.student.email]]
    row.refresh_from_db()
    assert row.status == "PENDING"
    assert ReminderLog.objects.get(item=item, kind="J3").status == "SENT"


# --------------------------------------------------------------------------- concurrency


@pytest.mark.django_db(transaction=True)
@pytest.mark.serial
def test_two_simultaneous_sendings_of_a_row_no_longer_due_skip_it_once(
    caplog: pytest.LogCaptureFixture,
) -> None:
    from apps.vault.tests.concurrency import run_threads

    caplog.set_level(logging.INFO, logger=LOGGER)
    team = make_team()
    row = _reminder(put(item_of(team), S.VALIDATED, team.student), team.student)
    barrier = threading.Barrier(2, timeout=5)

    def send() -> None:
        barrier.wait()
        tasks.send_reminder_email(str(row.pk))

    run_threads(send, send, control=lambda: None)
    row.refresh_from_db()
    assert row.status == "SKIPPED"
    assert mail.outbox == []
    assert len([r for r in caplog.records if r.name == LOGGER]) == 1
