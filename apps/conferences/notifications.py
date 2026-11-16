"""Notifications of the conferences: the meetings' mechanism (M5a).

notify() runs INSIDE the caller's transaction, before its audit(): one row per
recipient who is still an active internal member, queued after commit with its
id only. A row is unique per (kind, target, window, recipient): a reminder run
twice, or two runs at once, sends nothing twice. The e-mail is a link only
(CLAUDE.md rule 7).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from typing import Any

from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.accounts.models import Membership, Thesis, User
from apps.accounts.permissions import Action, can, can_access_level, roles_of
from apps.accounts.roles import Role
from apps.core import delivery
from apps.core.confidentiality import ConfidentialityLevel
from apps.meetings.members import internal_member_ids, is_internal_member

from .models import (
    ApprovalDecision,
    ApprovalPurpose,
    ConferenceNotification,
    Deadline,
    NotificationKind,
    NotificationStatus,
    StApproval,
    Submission,
    SubmissionAuthor,
)

MAX_ATTEMPTS = 3
K = NotificationKind
TUTOR_KINDS = frozenset({K.APPROVAL_PENDING, K.APPROVAL_REQUESTED})

# --------------------------------------------------------------------------- send-time check
# Checked by the sending task under the row's lock (apps.core.delivery): the
# state may have changed since the row was queued. services and reminders import
# this module: they are imported inside the functions.


def _reads(user: Any, submission: Submission) -> bool:
    return can_access_level(user, submission.thesis, ConfidentialityLevel(submission.level))


def _addressee(user: Any, roles: frozenset[Role], submission: Submission) -> bool:
    """The student, or an internal co-author of the submission, reading it."""
    if (
        Role.PHD_STUDENT not in roles
        and not SubmissionAuthor.objects.filter(submission=submission, user_id=user.pk).exists()
    ):
        return False
    return _reads(user, submission)


def _submission(row: ConferenceNotification) -> Submission | None:
    """The submission the row is about (its link, or its approval's)."""
    if row.kind in (K.REQUEST_DUE, K.APPROVAL_PENDING):
        link = row.link_id
        if link is None:  # always set for these kinds (notify's callers)
            return None
        return Submission.objects.filter(pk=link, thesis_id=row.thesis_id).first()
    approval = _approval(row)
    return approval.submission if approval is not None else None


def _approval(row: ConferenceNotification) -> StApproval | None:
    return (
        StApproval.objects.select_related("submission")
        .filter(pk=row.target_id, thesis_id=row.thesis_id)
        .first()
    )


def concerned(row: ConferenceNotification) -> bool:
    """An active internal member reading submissions, with the role of the kind:
    the student or a co-author (deadline), the student (request due, policy),
    the tutor (approval awaited), the requester (decision); reading the level."""
    user, thesis = row.recipient, row.thesis
    if not is_internal_member(user, thesis.pk) or not can(user, Action.SUBMISSION_READ, thesis):
        return False
    roles = roles_of(user, thesis)
    if row.kind == K.POLICY_CHANGED:
        return Role.PHD_STUDENT in roles
    if row.kind == K.DEADLINE:
        edition = row.link_id
        if edition is None:
            return False
        aimed = Submission.objects.filter(thesis=thesis, edition_id=edition)
        return any(_addressee(user, roles, s) for s in aimed)
    if row.kind not in (*TUTOR_KINDS, K.REQUEST_DUE, K.APPROVAL_DECIDED):
        return False
    submission = _submission(row)
    if submission is None or not _reads(user, submission):
        return False
    if row.kind == K.REQUEST_DUE:
        return Role.PHD_STUDENT in roles
    if row.kind in TUTOR_KINDS:
        return Role.INDUSTRIAL_TUTOR in roles
    approval = _approval(row)  # APPROVAL_DECIDED
    return approval is not None and approval.requested_by_id == user.pk


def still_due(row: ConferenceNotification) -> bool:
    """The deadline or the approval still calls for it: a deadline of the same
    instant, not cancelled, not passed, still relevant to a live submission of
    the recipient; a request due date of the same day, not passed, no approval
    requested; an approval still awaited, of the same reference deadline. A
    decision and a policy change happened: always due."""
    from . import reminders, services

    now = timezone.now()
    if row.kind in (K.POLICY_CHANGED, K.APPROVAL_DECIDED):
        return True
    if row.kind == K.APPROVAL_REQUESTED:
        approval = _approval(row)
        return approval is not None and approval.decision == ApprovalDecision.PENDING
    link = row.link_id
    if link is None:  # DEADLINE, REQUEST_DUE and APPROVAL_PENDING always have one
        return False
    if row.kind == K.DEADLINE:
        deadline = next(
            (
                d
                for d in Deadline.objects.filter(
                    edition_id=link, cancelled_at__isnull=True, due_at__gte=now
                )
                if reminders.event("deadline", d.pk, d.due_at.isoformat()) == row.target_id
            ),
            None,
        )
        if deadline is None:
            return False
        roles = roles_of(row.recipient, row.thesis)
        live = Submission.objects.filter(
            thesis=row.thesis, edition_id=link, status__in=list(reminders.RELEVANT)
        )
        return any(
            deadline.kind in reminders.RELEVANT[s.status] and _addressee(row.recipient, roles, s)
            for s in live
        )
    if row.kind == K.REQUEST_DUE:
        today = timezone.localdate()  # a Paris date, as the reminders
        return any(
            s.pk == link
            and due >= today
            and reminders.event("request", s.pk, due.isoformat()) == row.target_id
            for s, due in services.request_due_dates(row.thesis)
        )
    if row.kind == K.APPROVAL_PENDING:
        for approval in StApproval.objects.filter(
            submission_id=link, thesis_id=row.thesis_id, decision=ApprovalDecision.PENDING
        ).select_related("submission"):
            kind = services.REFERENCE_DEADLINE.get(ApprovalPurpose(approval.purpose))
            reference = services.reference_deadline(approval.submission, kind) if kind else None
            if (
                reference is not None
                and reference.due_at >= now
                and reminders.event("pending", approval.pk, reference.due_at.isoformat())
                == row.target_id
            ):
                return True
        return False
    return False


def skip_reason(row: ConferenceNotification) -> str | None:
    return delivery.skip_reason(
        row, thesis_open=row.thesis.is_open, concerned=concerned, still_due=still_due
    )


# --------------------------------------------------------------------------- queueing


def _queue(row: ConferenceNotification) -> None:
    from .tasks import send_conference_notification

    row_id = str(row.pk)  # the broker sees this id only
    transaction.on_commit(lambda: send_conference_notification.delay(row_id))


def notify(
    thesis: Thesis,
    kind: NotificationKind,
    target_id: uuid.UUID,
    recipients: Iterable[User],
    *,
    window: str = "",
    exclude: User | None = None,
    link_id: uuid.UUID | None = None,
) -> int:
    """Record and queue one notification per new (kind, target, window, recipient)."""
    members = internal_member_ids(thesis.pk)
    count = 0
    for user in {u.pk: u for u in recipients}.values():
        if user.pk not in members or (exclude is not None and user.pk == exclude.pk):
            continue
        try:
            with transaction.atomic():
                row = ConferenceNotification.objects.create(
                    thesis=thesis, kind=kind, target_id=target_id, window=window,
                    recipient=user, link_id=link_id,
                )  # fmt: skip
                _queue(row)
        except IntegrityError:  # already recorded (a twin run, or an earlier one)
            continue
        count += 1
    return count


def members_with_role(thesis: Thesis, role: Role) -> list[User]:
    ids = (
        Membership.objects.active()
        .filter(thesis=thesis, role=role, user__is_active=True)
        .values_list("user_id", flat=True)
    )
    return list(User.objects.filter(pk__in=list(ids)).order_by("pk"))


def requeue_failed() -> int:
    count = 0
    failed = ConferenceNotification.objects.filter(
        status=NotificationStatus.FAILED, attempts__lt=MAX_ATTEMPTS
    ).order_by("created_at", "pk")
    for row in failed:
        with transaction.atomic():
            _queue(row)
        count += 1
    return count
