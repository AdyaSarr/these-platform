"""Send-time check of the conferences' e-mails (branch notifications-still-due).

Under the row's lock, a ConferenceNotification is sent only if the thesis is
open, the recipient is still concerned (active internal member, the role of the
kind: student, internal co-author, tutor, requester; reading the submission) and
the deadline or the approval still calls for it; otherwise SKIPPED, nothing
sent, logged with the row id and a code only.

The objects come from the services (sealed texts: transaction=True); the rows
under test are created directly, then the state is changed before sending.
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
from apps.accounts.roles import Role
from apps.accounts.tests.factories import MembershipFactory
from apps.conferences import notifications, reminders, services, tasks
from apps.conferences.models import (
    ConferenceNotification,
    Deadline,
    StApproval,
    Submission,
    SubmissionAuthor,
    Venue,
)
from apps.conferences.tests.helpers import (
    NOW,
    D,
    L,
    T,
    article_version,
    deadline_of,
    edition_of,
    make_team,
    submission_of,
    venue_of,
)

pytestmark = pytest.mark.django_db(transaction=True)

K = ConferenceNotification.Kind
LOGGER = "apps.core.delivery"


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


# --------------------------------------------------------------------------- helpers


def _row(
    team: SimpleNamespace,
    kind: str,
    user: Any,
    target: uuid.UUID,
    *,
    link: uuid.UUID | None = None,
    window: str = "",
) -> ConferenceNotification:
    # The services may have recorded (and sent) this very event: start afresh.
    ConferenceNotification.objects.filter(
        kind=kind, target_id=target, window=window, recipient=user
    ).delete()
    return ConferenceNotification.objects.create(
        thesis=team.thesis, kind=kind, target_id=target, window=window, recipient=user,
        link_id=link,
    )  # fmt: skip


def _send(row: ConferenceNotification) -> str:
    mail.outbox.clear()
    tasks.send_conference_notification(str(row.pk))
    row.refresh_from_db()
    return str(row.status)


def _codes(log: Any) -> list[str]:
    return [r.getMessage() for r in log.records if r.name == LOGGER]


def _skipped(row: ConferenceNotification, code: str, log: Any) -> None:
    log.clear()
    assert _send(row) == "SKIPPED", row.kind
    assert mail.outbox == []
    assert _codes(log) == [f"conferences notification skipped ({row.pk}, {code})"]


def _end(user: Any, thesis: Thesis) -> None:
    Membership.objects.filter(user=user, thesis=thesis).update(active_until=timezone.now())


def _switch(user: Any, thesis: Thesis, role: Role) -> None:
    _end(user, thesis)
    MembershipFactory.create(thesis=thesis, user=user, role=role)


def _paper(team: SimpleNamespace, **kw: Any) -> tuple[Deadline, Submission]:
    """The PAPER deadline of a new edition (of a new venue) and a live submission."""
    acronym = f"SF{Venue.objects.filter(thesis=team.thesis).count()}"
    venue = venue_of(team, acronym=acronym, url=f"https://{acronym.lower()}.example.org/")
    edition = edition_of(team, venue=venue)
    deadline = deadline_of(team, edition)
    return deadline, submission_of(team, edition=edition, **kw)


def _deadline_row(team: SimpleNamespace, deadline: Deadline, user: Any) -> ConferenceNotification:
    target = reminders._event("deadline", deadline.pk, deadline.due_at.isoformat())
    return _row(team, K.DEADLINE, user, target, link=deadline.edition_id, window="J30")


def _request_due_row(team: SimpleNamespace, submission: Submission) -> ConferenceNotification:
    (due,) = [d for s, d in services.request_due_dates(team.thesis) if s.pk == submission.pk]
    target = reminders._event("request", submission.pk, due.isoformat())
    return _row(team, K.REQUEST_DUE, team.student, target, link=submission.pk, window="J30")


def _approval(team: SimpleNamespace, submission: Submission) -> StApproval:
    return services.request_approval(
        submission=submission, user=team.student, version=article_version(team, submission)
    )


def _pending_row(team: SimpleNamespace, approval: StApproval) -> ConferenceNotification:
    reference = services.reference_deadline(approval.submission, "PAPER")
    assert reference is not None
    target = reminders._event("pending", approval.pk, reference.due_at.isoformat())
    return _row(
        team, K.APPROVAL_PENDING, team.tutor, target, link=approval.submission_id, window="J7"
    )


def _decide(team: SimpleNamespace, approval: StApproval) -> StApproval:
    return services.decide_approval(
        approval=approval, user=team.tutor, decision="GRANTED", recent_mfa=True
    )


def _one_of_each(team: SimpleNamespace) -> list[ConferenceNotification]:
    """One row of each kind, each still due to a concerned recipient."""
    deadline, preparing = _paper(team)
    _, requested = _paper(team)
    approval = _approval(team, requested)
    _, granted = _paper(team)
    decided = _decide(team, _approval(team, granted))
    return [
        _deadline_row(team, deadline, team.student),
        _request_due_row(team, preparing),
        _pending_row(team, approval),
        _row(team, K.APPROVAL_REQUESTED, team.tutor, approval.pk),
        _row(team, K.APPROVAL_DECIDED, team.student, decided.pk),
        _row(team, K.POLICY_CHANGED, team.student, uuid.uuid4()),
    ]


# --------------------------------------------------------------------------- every kind


def test_every_kind_has_a_case_here() -> None:
    assert set(K.values) == {
        "DEADLINE",
        "REQUEST_DUE",
        "APPROVAL_PENDING",
        "APPROVAL_REQUESTED",
        "APPROVAL_DECIDED",
        "POLICY_CHANGED",
    }


def test_each_kind_still_due_is_sent(team: SimpleNamespace) -> None:
    for row in _one_of_each(team):
        assert _send(row) == "SENT", row.kind
        assert [m.to for m in mail.outbox] == [[row.recipient.email]]


@pytest.mark.parametrize("status", [Thesis.Status.DEFENDED, Thesis.Status.CLOSED])
def test_a_finished_thesis_skips_every_kind(team: SimpleNamespace, status: str, log: Any) -> None:
    rows = _one_of_each(team)
    Thesis.objects.filter(pk=team.thesis.pk).update(status=status)
    for row in rows:
        _skipped(row, "thesis_closed", log)


def test_an_ended_membership_with_an_active_account_skips_every_kind(
    team: SimpleNamespace, log: Any
) -> None:
    rows = _one_of_each(team)
    _end(team.student, team.thesis)
    _end(team.tutor, team.thesis)
    for row in rows:
        _skipped(row, "not_concerned", log)


# --------------------------------------------------------------------------- deadlines


def test_a_co_author_who_no_longer_reads_the_level_is_skipped(
    team: SimpleNamespace, log: Any
) -> None:
    deadline, submission = _paper(
        team, st_work=False, level=L.INTERNAL_LIRMM, authors=[team.co_supervisor]
    )
    row = _deadline_row(team, deadline, team.co_supervisor)
    Submission.objects.filter(pk=submission.pk).update(level=L.INTERNAL_ST)
    _skipped(row, "not_concerned", log)


def test_a_former_co_author_is_skipped(team: SimpleNamespace, log: Any) -> None:
    deadline, submission = _paper(team, authors=[team.tutor])
    row = _deadline_row(team, deadline, team.tutor)
    assert _send(_deadline_row(team, deadline, team.student)) == "SENT"
    SubmissionAuthor.objects.filter(submission=submission, user=team.tutor).delete()
    _skipped(row, "not_concerned", log)


def test_a_cancelled_deadline_is_skipped(team: SimpleNamespace, log: Any) -> None:
    deadline, _ = _paper(team)
    row = _deadline_row(team, deadline, team.student)
    services.cancel_deadline(deadline=deadline, user=team.student)
    _skipped(row, "not_due", log)


def test_a_moved_deadline_is_skipped(team: SimpleNamespace, log: Any) -> None:
    deadline, _ = _paper(team)
    row = _deadline_row(team, deadline, team.student)
    services.change_deadline(
        deadline=deadline, user=team.student, day=D(2027, 3, 8), wall=T(23, 59), tz_name="AoE"
    )
    _skipped(row, "not_due", log)


def test_a_passed_deadline_is_skipped(team: SimpleNamespace, log: Any, time_machine: Any) -> None:
    deadline, _ = _paper(team)
    row = _deadline_row(team, deadline, team.student)
    time_machine.move_to(deadline.due_at + dt.timedelta(minutes=1), tick=True)
    _skipped(row, "not_due", log)


@pytest.mark.parametrize("status", ["SUBMITTED", "WITHDRAWN", "REJECTED"])
def test_a_deadline_no_longer_relevant_to_the_submission_is_skipped(
    team: SimpleNamespace, status: str, log: Any
) -> None:
    """Once submitted, the PAPER deadline is behind; withdrawn or rejected, none."""
    deadline, submission = _paper(team)
    row = _deadline_row(team, deadline, team.student)
    Submission.objects.filter(pk=submission.pk).update(status=status, submitted_at=timezone.now())
    _skipped(row, "not_due", log)


# --------------------------------------------------------------------------- request due


def test_a_request_due_once_requested_is_skipped(team: SimpleNamespace, log: Any) -> None:
    _, submission = _paper(team)
    row = _request_due_row(team, submission)
    _approval(team, submission)
    _skipped(row, "not_due", log)


def test_a_request_due_of_work_no_longer_st_is_skipped(team: SimpleNamespace, log: Any) -> None:
    _, submission = _paper(team)
    row = _request_due_row(team, submission)
    Submission.objects.filter(pk=submission.pk).update(st_work=False)
    _skipped(row, "not_due", log)


def test_a_request_due_of_a_moved_deadline_is_skipped(team: SimpleNamespace, log: Any) -> None:
    deadline, submission = _paper(team)
    row = _request_due_row(team, submission)
    services.change_deadline(
        deadline=deadline, user=team.student, day=D(2027, 3, 8), wall=T(23, 59), tz_name="AoE"
    )
    _skipped(row, "not_due", log)


def test_a_request_due_to_a_former_student_is_skipped(team: SimpleNamespace, log: Any) -> None:
    _, submission = _paper(team)
    row = _request_due_row(team, submission)
    _switch(team.student, team.thesis, Role.GUEST)
    _skipped(row, "not_concerned", log)


# --------------------------------------------------------------------------- approvals


def test_an_approval_pending_once_decided_is_skipped(team: SimpleNamespace, log: Any) -> None:
    _, submission = _paper(team)
    approval = _approval(team, submission)
    pending, requested = (
        _pending_row(team, approval),
        _row(team, K.APPROVAL_REQUESTED, team.tutor, approval.pk),
    )
    _decide(team, approval)
    _skipped(pending, "not_due", log)
    _skipped(requested, "not_due", log)


def test_an_approval_withdrawn_meanwhile_is_skipped(team: SimpleNamespace, log: Any) -> None:
    _, submission = _paper(team)
    approval = _approval(team, submission)
    pending, requested = (
        _pending_row(team, approval),
        _row(team, K.APPROVAL_REQUESTED, team.tutor, approval.pk),
    )
    services.withdraw_approval(approval=approval, user=team.student)
    _skipped(pending, "not_due", log)
    _skipped(requested, "not_due", log)


def test_an_approval_pending_of_a_moved_reference_deadline_is_skipped(
    team: SimpleNamespace, log: Any
) -> None:
    deadline, submission = _paper(team)
    row = _pending_row(team, _approval(team, submission))
    services.change_deadline(
        deadline=deadline, user=team.student, day=D(2027, 3, 8), wall=T(23, 59), tz_name="AoE"
    )
    _skipped(row, "not_due", log)


@pytest.mark.parametrize("role", [Role.DIRECTOR, Role.GUEST])
def test_a_tutor_who_changed_role_is_skipped(team: SimpleNamespace, role: Role, log: Any) -> None:
    _, submission = _paper(team)
    approval = _approval(team, submission)
    rows = [
        _pending_row(team, approval),
        _row(team, K.APPROVAL_REQUESTED, team.tutor, approval.pk),
    ]
    _switch(team.tutor, team.thesis, role)
    for row in rows:
        _skipped(row, "not_concerned", log)


def test_a_decision_is_skipped_for_someone_else_than_the_requester(
    team: SimpleNamespace, log: Any
) -> None:
    _, submission = _paper(team)
    decided = _decide(team, _approval(team, submission))
    _skipped(_row(team, K.APPROVAL_DECIDED, team.director, decided.pk), "not_concerned", log)


def test_a_decision_stays_due_after_a_later_change(team: SimpleNamespace) -> None:
    """An accomplished event: the submission moving on does not cancel it."""
    _, submission = _paper(team)
    decided = _decide(team, _approval(team, submission))
    row = _row(team, K.APPROVAL_DECIDED, team.student, decided.pk)
    Submission.objects.filter(pk=submission.pk).update(status="WITHDRAWN")
    assert _send(row) == "SENT"


def test_a_policy_change_to_a_former_student_is_skipped(team: SimpleNamespace, log: Any) -> None:
    row = _row(team, K.POLICY_CHANGED, team.student, uuid.uuid4())
    _switch(team.student, team.thesis, Role.GUEST)
    _skipped(row, "not_concerned", log)


# --------------------------------------------------------------------------- others


def test_a_skipped_row_queued_again_logs_once(team: SimpleNamespace, log: Any) -> None:
    row = _row(team, K.POLICY_CHANGED, team.student, uuid.uuid4())
    _end(team.student, team.thesis)
    _skipped(row, "not_concerned", log)
    assert _send(row) == "SKIPPED"
    assert len(_codes(log)) == 1


def test_an_unknown_kind_is_never_concerned(team: SimpleNamespace) -> None:
    row = _row(team, K.POLICY_CHANGED, team.student, uuid.uuid4())
    row.kind = "UNKNOWN"  # in memory only: the database refuses it (CHECK)
    assert notifications.concerned(row) is False
    assert notifications.still_due(row) is False
