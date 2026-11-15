"""Daily reminders of the conferences: J-30, J-14, J-7, J-1.

Run once a day by `manage.py conferences_reminders` (systemd timer,
docs/DEPLOYMENT.md). Days are DATES, never hours:
- a deadline counts in its own zone: days = (deadline's date in its zone) -
  (the date of the run in that zone). At 07:00 in Paris it is still the day
  before in AoE; a change to summer time moves nothing;
- the request due date of an ST approval is a Paris date: counted in Paris.
Only the window of the day is sent (J-30 from 30 to 15 days, J-14 from 14 to 8,
J-7 from 7 to 2, J-1 from 1 to 0), each once per (event, window, recipient): a
run repeated, or two at once, sends nothing twice. A moved deadline is a new
event (its id and its instant), reminded again.

Who: deadlines of the editions aimed at by a live submission, by its status
(abstract and paper before submitting, rebuttal once submitted, final version
and registration once accepted) -> the student and the internal co-authors;
request due dates -> the student; approvals awaiting a decision -> the tutors
(J-7 and J-1 of the reference deadline). Active internal members of an open
thesis only; the e-mail is a link (CLAUDE.md rule 7).
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass

from django.utils import timezone

from apps.accounts.models import Thesis, User
from apps.accounts.roles import Role
from apps.core import timezones

from . import services
from .models import (
    ApprovalDecision,
    ApprovalPurpose,
    Deadline,
    DeadlineKind,
    NotificationKind,
    StApproval,
    Submission,
    SubmissionAuthor,
    SubmissionStatus,
    Window,
)
from .notifications import members_with_role, notify, requeue_failed

OPEN_THESIS = (Thesis.Status.SETUP, Thesis.Status.ACTIVE)
# Fixed namespace of the reminders' event ids (uuid5): never change it.
EVENT_NAMESPACE = uuid.UUID("2c6f1e0a-8d4b-4f3e-9b7a-5e1d3c9f7a20")
S = SubmissionStatus
RELEVANT: dict[str, frozenset[str]] = {
    S.PREPARING: frozenset({DeadlineKind.ABSTRACT, DeadlineKind.PAPER}),
    S.ST_REQUESTED: frozenset({DeadlineKind.ABSTRACT, DeadlineKind.PAPER}),
    S.ST_GRANTED: frozenset({DeadlineKind.ABSTRACT, DeadlineKind.PAPER}),
    S.SUBMITTED: frozenset({DeadlineKind.REBUTTAL}),
    S.ACCEPTED: frozenset({DeadlineKind.CAMERA_READY, DeadlineKind.REGISTRATION}),
}
TUTOR_WINDOWS = frozenset({Window.J7, Window.J1})


@dataclass(frozen=True)
class RunResult:
    deadlines: int
    request_dues: int
    pending: int
    requeued: int


def window_for(days: int) -> str | None:
    if 15 <= days <= 30:
        return Window.J30
    if 8 <= days <= 14:
        return Window.J14
    if 2 <= days <= 7:
        return Window.J7
    if 0 <= days <= 1:
        return Window.J1
    return None


def _event(*parts: object) -> uuid.UUID:
    return uuid.uuid5(EVENT_NAMESPACE, ":".join(str(p) for p in parts))


def _deadline_window(deadline: Deadline, now: dt.datetime) -> str | None:
    days = (
        timezones.local_date(deadline.due_at, deadline.tz_name)
        - timezones.local_date(now, deadline.tz_name)
    ).days
    return window_for(days)


def _authors(submission: Submission) -> list[User]:
    ids = SubmissionAuthor.objects.filter(submission=submission).values_list("user_id", flat=True)
    return list(User.objects.filter(pk__in=list(ids)))


def run(now: dt.datetime | None = None) -> RunResult:
    if now is not None and (not isinstance(now, dt.datetime) or now.tzinfo is None):
        raise ValueError("run() needs an aware datetime")
    now = now or timezone.now()
    # Failed e-mails of earlier runs first: a row created below is tried once per run.
    requeued = requeue_failed()
    paris_today = timezones.local_date(now, "Europe/Paris")
    deadlines = request_dues = pending = 0
    for thesis in Thesis.objects.filter(status__in=OPEN_THESIS).order_by("pk"):
        students = members_with_role(thesis, Role.PHD_STUDENT)
        live = Submission.objects.filter(thesis=thesis, status__in=list(RELEVANT)).order_by("pk")
        for submission in live:
            if submission.edition_id is None:
                continue
            kinds = RELEVANT[submission.status]
            recipients = students + _authors(submission)
            for deadline in Deadline.objects.filter(
                edition_id=submission.edition_id,
                kind__in=list(kinds),
                cancelled_at__isnull=True,
                due_at__gte=now,
            ).order_by("due_at"):
                window = _deadline_window(deadline, now)
                if window is None:
                    continue
                deadlines += notify(
                    thesis,
                    NotificationKind.DEADLINE,
                    _event("deadline", deadline.pk, deadline.due_at.isoformat()),
                    recipients,
                    window=window,
                    link_id=deadline.edition_id,
                )
        for submission, due in services.request_due_dates(thesis):
            window = window_for((due - paris_today).days)
            if window is not None:
                request_dues += notify(
                    thesis,
                    NotificationKind.REQUEST_DUE,
                    _event("request", submission.pk, due.isoformat()),
                    students,
                    window=window,
                    link_id=submission.pk,
                )
        tutors = members_with_role(thesis, Role.INDUSTRIAL_TUTOR)
        for approval in StApproval.objects.filter(
            thesis=thesis, decision=ApprovalDecision.PENDING
        ).select_related("submission"):
            kind = services.REFERENCE_DEADLINE.get(ApprovalPurpose(approval.purpose))
            reference = services.reference_deadline(approval.submission, kind) if kind else None
            if reference is None or reference.due_at < now:
                continue
            window = _deadline_window(reference, now)
            if window not in TUTOR_WINDOWS:
                continue
            pending += notify(
                thesis,
                NotificationKind.APPROVAL_PENDING,
                _event("pending", approval.pk, reference.due_at.isoformat()),
                tutors,
                window=window or "",
                link_id=approval.submission_id,
            )
    return RunResult(deadlines, request_dues, pending, requeued)
