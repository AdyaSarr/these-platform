"""Business logic of the conferences and submissions (M6).

Rights are decided HERE (accounts.permissions), even when a view checked too:
- catalogue (venues, editions, deadlines): CONFERENCE_READ / CONFERENCE_WRITE;
- submissions: SUBMISSION_READ (texts at their level) / SUBMISSION_WRITE (the
  student: create, edit, status, documents, approval requests, preprints, level);
- ST publication approval: VALIDATE_PUBLICATION (the industrial tutor) decides,
  turns ``st_work`` off and sets the review period, each with a second factor
  less than 10 minutes old (``recent_mfa``, given by the view).
A refusal is audited (CONFERENCE_ACCESS_DENIED) in its own transaction.

The rule of the ST approval: every public act — submitting, publishing the final
version, posting a preprint — names an exact DocumentVersion of a linked document
and needs, when ``st_work``, a GRANTED approval of that version for this
submission. An approval is never taken from another submission.

Texts go through the vault at the submission's level; the audit log gets codes,
dates (ISO, UTC instants) and ids, never a text. Lock order (CLAUDE.md): KEK lock
(vault.sealing), thesis row, submission row, then audit() LAST. Times: deadlines
in UTC with their zone (core.timezones); the request due date of an approval is a
DATE in Paris (reference deadline's Paris date - review period).
"""

from __future__ import annotations

import datetime as dt
import unicodedata
import uuid
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any, NamedTuple

from django.core.exceptions import PermissionDenied, ValidationError
from django.core.validators import URLValidator
from django.db import IntegrityError, transaction
from django.utils import timezone
from django.utils.translation import gettext as _

from apps.accounts.models import Thesis, User
from apps.accounts.permissions import (
    LEVEL_ROLES,
    Action,
    can,
    can_access_document,
    roles_of,
)
from apps.accounts.roles import Role
from apps.activity import events as activity
from apps.core import timezones
from apps.core.confidentiality import ConfidentialityLevel
from apps.meetings.members import INTERNAL_ROLES
from apps.objectives.models import Objective
from apps.vault import formats
from apps.vault import services as vault
from apps.vault.audit import AuditAction, audit
from apps.vault.errors import ReauthenticationRequiredError
from apps.vault.models import Document, DocumentVersion

from .models import (
    MAX_REVIEW_DAYS,
    ST_LEVELS,
    ApprovalDecision,
    ApprovalPurpose,
    Deadline,
    DeadlineKind,
    Edition,
    NotificationKind,
    Preprint,
    PreprintPlatform,
    PublicationPolicy,
    RefusalReason,
    StApproval,
    Submission,
    SubmissionAuthor,
    SubmissionDocument,
    SubmissionStatus,
    Venue,
    VenueKind,
)
from .notifications import members_with_role, notify

OPEN_THESIS = (Thesis.Status.SETUP, Thesis.Status.ACTIVE)
DEFAULT_REVIEW_DAYS = 30
SUBMISSION_TEXTS = ("title", "abstract", "external_authors", "notes")
TEXT_LIMITS = {
    "acronym": (1, 32),
    "name": (1, 200),
    "location": (0, 200),
    "url": (0, 300),
    "title": (1, 300),
    "abstract": (0, 8000),
    "external_authors": (0, 2000),
    "notes": (0, 8000),
    "comment": (0, 2000),
    "preprint_url": (1, 500),
}
_ALLOWED_CONTROLS = frozenset("\n\t")
_https = URLValidator(schemes=["https"])
S = SubmissionStatus
REFERENCE_DEADLINE = {
    ApprovalPurpose.SUBMISSION: DeadlineKind.PAPER,
    ApprovalPurpose.CAMERA_READY: DeadlineKind.CAMERA_READY,
}


# =========================================================================== validation


def _clean_text(value: Any, code: str) -> str:
    """NFC, CRLF -> LF, stripped; no control character but newline and tab; bounded."""
    minimum, maximum = TEXT_LIMITS[code]
    if not isinstance(value, str):
        raise ValidationError(_("This text is invalid."), code=code)
    text = unicodedata.normalize("NFC", value.replace("\r\n", "\n")).strip()
    if any(unicodedata.category(c) == "Cc" and c not in _ALLOWED_CONTROLS for c in text):
        raise ValidationError(_("The text contains a control character."), code=code)
    if not minimum <= len(text) <= maximum or len(text.encode()) > formats.MAX_TEXT_BYTES:
        raise ValidationError(
            _("Between %(min)d and %(max)d characters.") % {"min": minimum, "max": maximum},
            code=code,
        )
    return text


def _clean_line(value: Any, code: str) -> str:
    text = _clean_text(value, code)
    if "\n" in text or "\t" in text:
        raise ValidationError(_("One line only."), code=code)
    return text


def _clean_url(value: Any, code: str = "url", limit: str = "url") -> str:
    url = _clean_line(value, limit)
    if url:
        try:
            _https(url)
        except ValidationError:
            raise ValidationError(_("The address must start with https://."), code=code) from None
    return url


def _clean_level(level: Any) -> ConfidentialityLevel:
    if not isinstance(level, ConfidentialityLevel):
        raise ValidationError(_("Unknown confidentiality level."), code="level")
    return level


def _check_st_level(st_work: bool, level: ConfidentialityLevel) -> None:
    if st_work and level.value not in ST_LEVELS:
        raise ValidationError(
            _("A submission on ST work must be readable by the industrial tutor."),
            code="st_level",
        )


def _check_authors(
    thesis: Thesis, users: Iterable[User], level: ConfidentialityLevel
) -> list[User]:
    people = list({u.pk: u for u in users}.values())
    for user in people:
        roles = roles_of(user, thesis) & INTERNAL_ROLES
        if not roles:
            raise ValidationError(
                _("A co-author must be an active member of the supervision team."),
                code="author_not_member",
            )
        if not roles & LEVEL_ROLES[level]:
            raise ValidationError(
                _("A co-author could not read this confidentiality level."), code="author_level"
            )
    return people


def _check_target(thesis: Thesis, venue: Venue, edition: Edition | None) -> None:
    if venue.thesis_id != thesis.pk or (edition is not None and edition.thesis_id != thesis.pk):
        raise ValidationError(_("This venue belongs to another thesis."), code="venue")
    if venue.is_journal:
        if edition is not None:
            raise ValidationError(_("A journal has no edition."), code="edition")
        return
    if edition is None or edition.venue_id != venue.pk:
        raise ValidationError(_("Choose an edition of this conference."), code="edition")


def _check_objective(thesis: Thesis, objective: Objective | None) -> Objective | None:
    if objective is not None and objective.thesis_id != thesis.pk:
        raise ValidationError(_("This objective belongs to another thesis."), code="objective")
    return objective


def _from_to(previous: Any, new: Any) -> dict[str, Any]:
    """Audit details ``from`` / ``to`` (``from`` is a keyword in Python)."""
    return {"from": previous, "to": new}


def _iso(instant: dt.datetime) -> str:
    return instant.astimezone(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


# =========================================================================== rights


def _deny(user: Any, operation: str, reason: str, **kwargs: Any) -> PermissionDenied:
    with transaction.atomic():
        audit(
            AuditAction.CONFERENCE_ACCESS_DENIED,
            actor=user,
            operation=operation,
            reason=reason,
            **kwargs,
        )
    return PermissionDenied()


def deny_unknown(user: Any, thesis: Thesis, what: str) -> PermissionDenied:
    """An id of another thesis (or none) in a URL: audited, the same 403."""
    return _deny(user, what, f"unknown_{what}", thesis=thesis)


def _require(user: Any, action: Action, thesis: Thesis, operation: str) -> None:
    if not can(user, action, thesis):
        raise _deny(user, operation, "missing_right", thesis=thesis)


def _thesis(obj: Any) -> Thesis:
    return Thesis.objects.get(pk=obj.thesis_id)


def _frozen() -> ValidationError:
    return ValidationError(_("The thesis is finished: its submissions are frozen."), code="frozen")


def _check_open(thesis: Thesis) -> None:
    if Thesis.objects.get(pk=thesis.pk).status not in OPEN_THESIS:
        raise _frozen()


def _lock_thesis(thesis_id: Any) -> Thesis:
    thesis = Thesis.objects.select_for_update().get(pk=thesis_id)
    if thesis.status not in OPEN_THESIS:
        raise _frozen()
    return thesis


def _locked(submission: Submission) -> Submission:
    _lock_thesis(submission.thesis_id)
    return Submission.objects.select_for_update().get(pk=submission.pk)


def _level(submission: Submission) -> ConfidentialityLevel:
    return ConfidentialityLevel(submission.level)


def _transition() -> ValidationError:
    return ValidationError(_("This change of status is not possible now."), code="transition")


def _require_mfa(recent_mfa: bool) -> None:
    if not recent_mfa:
        raise ReauthenticationRequiredError()


# =========================================================================== catalogue


def create_venue(
    *, thesis: Thesis, user: User, kind: str, acronym: str, name: str, url: str = ""
) -> Venue:
    _require(user, Action.CONFERENCE_WRITE, thesis, "venue")
    _check_open(thesis)
    if kind not in VenueKind.values:
        raise ValidationError(_("Unknown type."), code="kind")
    values = {
        "acronym": _clean_line(acronym, "acronym"),
        "name": _clean_line(name, "name"),
        "url": _clean_url(url),
    }
    if Venue.objects.filter(thesis=thesis, kind=kind, acronym=values["acronym"]).exists():
        raise ValidationError(_("This venue already exists."), code="duplicate")
    with transaction.atomic():
        _lock_thesis(thesis.pk)
        venue = Venue.objects.create(thesis=thesis, kind=kind, created_by=user, **values)
        audit(AuditAction.CONFERENCE_VENUE_CREATED, actor=user, obj=venue, kind=kind)
    return venue


def update_venue(*, venue: Venue, user: User, changes: Mapping[str, Any]) -> list[str]:
    thesis = _thesis(venue)
    _require(user, Action.CONFERENCE_WRITE, thesis, "venue")
    _check_open(thesis)
    cleaners: dict[str, Callable[[Any], str]] = {
        "acronym": lambda v: _clean_line(v, "acronym"),
        "name": lambda v: _clean_line(v, "name"),
        "url": _clean_url,
    }
    unknown = sorted(set(changes) - set(cleaners))
    if unknown:
        raise ValueError(f"{unknown} cannot be changed here")
    values = {name: cleaners[name](value) for name, value in changes.items()}
    with transaction.atomic():
        _lock_thesis(thesis.pk)
        locked = Venue.objects.select_for_update().get(pk=venue.pk)
        changed = [n for n, v in values.items() if getattr(locked, n) != v]
        if not changed:
            return []
        for name in changed:
            setattr(locked, name, values[name])
        try:
            with transaction.atomic():
                locked.save()
        except IntegrityError:
            raise ValidationError(_("This venue already exists."), code="duplicate") from None
        audit(AuditAction.CONFERENCE_VENUE_UPDATED, actor=user, obj=locked, fields=changed)
    return changed


def _clean_edition(values: Mapping[str, Any]) -> dict[str, Any]:
    year = values["year"]
    if type(year) is not int or not 2000 <= year <= 2100:
        raise ValidationError(_("A year between 2000 and 2100."), code="year")
    start, end = values.get("event_start"), values.get("event_end")
    for day in (start, end):
        if day is not None and (isinstance(day, dt.datetime) or not isinstance(day, dt.date)):
            raise ValidationError(_("A date is required."), code="event_dates")
    if start is not None and end is not None and start > end:
        raise ValidationError(_("The event ends before it starts."), code="event_dates")
    return {
        "year": year,
        "location": _clean_line(values.get("location", ""), "location"),
        "event_start": start,
        "event_end": end,
    }


def create_edition(
    *,
    venue: Venue,
    user: User,
    year: int,
    location: str = "",
    event_start: dt.date | None = None,
    event_end: dt.date | None = None,
) -> Edition:
    thesis = _thesis(venue)
    _require(user, Action.CONFERENCE_WRITE, thesis, "edition")
    _check_open(thesis)
    if venue.is_journal:
        raise ValidationError(_("A journal has no edition."), code="journal")
    values = _clean_edition(
        {"year": year, "location": location, "event_start": event_start, "event_end": event_end}
    )
    if Edition.objects.filter(venue=venue, year=values["year"]).exists():
        raise ValidationError(_("This edition already exists."), code="duplicate")
    with transaction.atomic():
        _lock_thesis(thesis.pk)
        edition = Edition.objects.create(thesis=thesis, venue=venue, created_by=user, **values)
        audit(AuditAction.CONFERENCE_EDITION_CREATED, actor=user, obj=edition, year=edition.year)
    return edition


def add_deadline(
    *, edition: Edition, user: User, kind: str, day: dt.date, wall: dt.time, tz_name: str
) -> Deadline:
    """A deadline typed in its own zone ("AoE" or IANA), stored in UTC."""
    thesis = _thesis(edition)
    _require(user, Action.CONFERENCE_WRITE, thesis, "deadline")
    _check_open(thesis)
    if kind not in DeadlineKind.values:
        raise ValidationError(_("Unknown type."), code="kind")
    due_at = timezones.local_to_utc(day, wall, tz_name)
    with transaction.atomic():
        _lock_thesis(thesis.pk)
        deadline = Deadline.objects.create(
            thesis=thesis, edition=edition, kind=kind, due_at=due_at, tz_name=tz_name,
            created_by=user,
        )  # fmt: skip
        audit(
            AuditAction.DEADLINE_ADDED,
            actor=user,
            obj=deadline,
            kind=kind,
            due_at=_iso(due_at),
            tz_name=tz_name,
        )
    return deadline


def change_deadline(
    *, deadline: Deadline, user: User, day: dt.date, wall: dt.time, tz_name: str
) -> Deadline:
    thesis = _thesis(deadline)
    _require(user, Action.CONFERENCE_WRITE, thesis, "deadline")
    _check_open(thesis)
    due_at = timezones.local_to_utc(day, wall, tz_name)
    with transaction.atomic():
        _lock_thesis(thesis.pk)
        locked = Deadline.objects.select_for_update().get(pk=deadline.pk)
        if locked.cancelled_at is not None:
            raise ValidationError(_("This deadline was cancelled."), code="cancelled")
        if (locked.due_at, locked.tz_name) == (due_at, tz_name):
            return locked
        previous = locked.due_at
        locked.due_at, locked.tz_name = due_at, tz_name
        locked.save()
        audit(
            AuditAction.DEADLINE_CHANGED,
            actor=user,
            obj=locked,
            **_from_to(_iso(previous), _iso(due_at)),
            tz_name=tz_name,
        )
    return locked


def cancel_deadline(*, deadline: Deadline, user: User) -> Deadline:
    thesis = _thesis(deadline)
    _require(user, Action.CONFERENCE_WRITE, thesis, "deadline")
    _check_open(thesis)
    with transaction.atomic():
        _lock_thesis(thesis.pk)
        locked = Deadline.objects.select_for_update().get(pk=deadline.pk)
        if locked.cancelled_at is not None:
            raise ValidationError(_("This deadline was cancelled."), code="cancelled")
        locked.cancelled_at = timezone.now()
        locked.save(update_fields=["cancelled_at", "updated_at"])
        audit(AuditAction.DEADLINE_CANCELLED, actor=user, obj=locked)
    return locked


class DeadlineDisplay(NamedTuple):
    deadline: Deadline
    paris: tuple[dt.date, dt.time]  # in Paris
    local: tuple[dt.date, dt.time]  # in its own zone
    tz_name: str


def deadline_display(deadline: Deadline) -> DeadlineDisplay:
    return DeadlineDisplay(
        deadline=deadline,
        paris=timezones.local_parts(deadline.due_at, "Europe/Paris"),
        local=timezones.local_parts(deadline.due_at, deadline.tz_name),
        tz_name=deadline.tz_name,
    )


def upcoming_deadlines(user: User, thesis: Thesis, *, limit: int | None = None) -> list[Deadline]:
    _require(user, Action.CONFERENCE_READ, thesis, "deadlines")
    rows = (
        Deadline.objects.filter(
            thesis=thesis, cancelled_at__isnull=True, due_at__gte=timezone.now()
        )
        .select_related("edition__venue")
        .order_by("due_at", "pk")
    )
    return list(rows[:limit] if limit else rows)


# =========================================================================== policy


def review_days(thesis: Thesis) -> int:
    policy = PublicationPolicy.objects.filter(thesis=thesis).first()
    return policy.st_review_days if policy else DEFAULT_REVIEW_DAYS


def set_review_days(*, thesis: Thesis, user: User, days: int, recent_mfa: bool) -> int:
    """The industrial tutor sets the ST review period; the student is told (a link).
    Requests already made keep the period they were made with."""
    _require(user, Action.VALIDATE_PUBLICATION, thesis, "policy")
    _require_mfa(recent_mfa)
    if type(days) is not int or not 0 <= days <= MAX_REVIEW_DAYS:
        raise ValidationError(
            _("Between 0 and %(max)d days.") % {"max": MAX_REVIEW_DAYS}, code="days"
        )
    _check_open(thesis)
    with transaction.atomic():
        _lock_thesis(thesis.pk)
        policy, _created = PublicationPolicy.objects.select_for_update().get_or_create(
            thesis=thesis, defaults={"created_by": user}
        )
        previous = policy.st_review_days
        if previous == days:
            return days
        policy.st_review_days = days
        policy.save()
        notify(
            thesis,
            NotificationKind.POLICY_CHANGED,
            uuid.uuid4(),
            members_with_role(thesis, Role.PHD_STUDENT),
            exclude=user,
        )
        audit(
            AuditAction.PUBLICATION_POLICY_CHANGED,
            actor=user,
            obj=policy,
            **_from_to(previous, days),
        )
    return days


# =========================================================================== reading


def submission_for(user: User, thesis: Thesis, submission_id: Any, *, operation: str) -> Submission:
    """The submission of ``thesis`` (never of another one, IDOR) if readable."""
    try:
        submission = Submission.objects.filter(pk=submission_id, thesis=thesis).first()
    except (ValueError, ValidationError):
        submission = None
    if submission is None:
        raise _deny(user, operation, "unknown_submission", thesis=thesis)
    _require(user, Action.SUBMISSION_READ, thesis, operation)
    return submission


@dataclass(frozen=True)
class ApprovalView:
    approval: StApproval
    comment: Any  # str or vault.RESTRICTED


@dataclass(frozen=True)
class PreprintView:
    preprint: Preprint
    url: Any


@dataclass(frozen=True)
class SubmissionDetail:
    submission: Submission
    texts: dict[str, Any]
    authors: list[User]
    approvals: list[ApprovalView]
    preprints: list[PreprintView]
    documents: list[tuple[Document, SubmissionDocument]]
    hidden_documents: int
    deadlines: list[Deadline]


def submission_detail(user: User, submission: Submission) -> SubmissionDetail:
    """Everything the page shows, its texts opened in ONE vault call."""
    thesis = _thesis(submission)
    _require(user, Action.SUBMISSION_READ, thesis, "detail")
    submission = Submission.objects.select_related("venue", "edition").get(pk=submission.pk)
    approvals = list(
        StApproval.objects.filter(submission=submission)
        .select_related("version__document", "requested_by", "decided_by")
        .order_by("created_at", "pk")
    )
    preprints = list(Preprint.objects.filter(submission=submission).order_by("posted_on", "pk"))
    items: list[tuple[Any, str]] = [(submission, n) for n in SUBMISSION_TEXTS]
    items += [(a, "comment") for a in approvals]
    items += [(p, "url") for p in preprints]
    opened = vault.open_texts(user, thesis, items, action=Action.SUBMISSION_READ)
    links = list(
        SubmissionDocument.objects.filter(submission=submission)
        .select_related("document")
        .order_by("created_at", "pk")
    )
    shown = [link for link in links if can_access_document(user, link.document)]
    author_ids = SubmissionAuthor.objects.filter(submission=submission).values_list(
        "user_id", flat=True
    )
    deadlines = (
        list(
            Deadline.objects.filter(edition=submission.edition, cancelled_at__isnull=True).order_by(
                "due_at"
            )
        )
        if submission.edition_id
        else []
    )
    return SubmissionDetail(
        submission=submission,
        texts={n: opened.get((submission.pk, n), "") for n in SUBMISSION_TEXTS},
        authors=list(User.objects.filter(pk__in=list(author_ids)).order_by("last_name", "email")),
        approvals=[ApprovalView(a, opened.get((a.pk, "comment"), "")) for a in approvals],
        preprints=[PreprintView(p, opened.get((p.pk, "url"), "")) for p in preprints],
        documents=[(link.document, link) for link in shown],
        hidden_documents=len(links) - len(shown),
        deadlines=deadlines,
    )


@dataclass(frozen=True)
class SubmissionRow:
    submission: Submission
    title: Any


def submission_list(user: User, thesis: Thesis) -> list[SubmissionRow]:
    _require(user, Action.SUBMISSION_READ, thesis, "list")
    submissions = list(
        Submission.objects.filter(thesis=thesis)
        .select_related("venue", "edition")
        .order_by("-created_at", "pk")
    )
    opened = vault.open_texts(
        user, thesis, [(s, "title") for s in submissions], action=Action.SUBMISSION_READ
    )
    return [SubmissionRow(s, opened.get((s.pk, "title"), "")) for s in submissions]


def submissions_for_objective(user: User, objective: Objective) -> list[SubmissionRow]:
    thesis = _thesis(objective)
    _require(user, Action.SUBMISSION_READ, thesis, "objective_submissions")
    submissions = list(
        Submission.objects.filter(thesis=thesis, objective=objective)
        .select_related("venue", "edition")
        .order_by("created_at", "pk")
    )
    opened = vault.open_texts(
        user, thesis, [(s, "title") for s in submissions], action=Action.SUBMISSION_READ
    )
    return [SubmissionRow(s, opened.get((s.pk, "title"), "")) for s in submissions]


def pending_approvals(user: User, thesis: Thesis) -> list[StApproval]:
    """The approvals awaiting the tutor's decision (none for other roles)."""
    if not can(user, Action.VALIDATE_PUBLICATION, thesis):
        return []
    return list(
        StApproval.objects.filter(thesis=thesis, decision=ApprovalDecision.PENDING)
        .select_related("submission")
        .order_by("request_due", "created_at")
    )


def _active(thesis: Thesis) -> list[Submission]:
    return list(
        Submission.objects.filter(thesis=thesis).exclude(
            status__in=[s.value for s in (S.REJECTED, S.WITHDRAWN, S.PUBLISHED)]
        )
    )


def request_due_dates(thesis: Thesis) -> list[tuple[Submission, dt.date]]:
    """The date by which ST's approval should be requested, for the active
    submissions on ST work that have no approval of their submission yet."""
    days = review_days(thesis)
    result = []
    for submission in _active(thesis):
        if not submission.st_work or submission.status not in (S.PREPARING,):
            continue
        due = _request_due(submission, ApprovalPurpose.SUBMISSION, days)
        if due is not None:
            result.append((submission, due))
    return sorted(result, key=lambda pair: pair[1])


def submission_counts(user: Any, thesis: Thesis) -> list[tuple[str, int]]:
    """(status label, number) of the submissions of ``thesis``, in the order of the
    statuses, the empty ones left out. Read only; no text opened (the list of the
    submissions shows them all to SUBMISSION_READ, titles by level)."""
    _require(user, Action.SUBMISSION_READ, thesis, "counts")
    counts: dict[str, int] = {}
    for status in Submission.objects.filter(thesis=thesis).values_list("status", flat=True):
        counts[status] = counts.get(status, 0) + 1
    return [(str(s.label), counts[s.value]) for s in S if s.value in counts]


def dashboard_block(request: Any, thesis: Thesis) -> dict[str, Any]:
    """The « Conferences » card: next deadlines of the targeted editions, request
    due dates, approvals awaiting the tutor."""
    user = request.user
    _require(user, Action.CONFERENCE_READ, thesis, "dashboard")
    targeted = {s.edition_id for s in _active(thesis) if s.edition_id}
    deadlines = [
        deadline_display(d) for d in upcoming_deadlines(user, thesis) if d.edition_id in targeted
    ][:5]
    today = timezone.localdate()
    pending = pending_approvals(user, thesis)  # the tutor's only
    return {
        "deadlines": deadlines,
        "request_dues": [
            (s, due, (due - today).days) for s, due in request_due_dates(thesis) if due >= today
        ][:3],
        "pending": len(pending),
        "pending_approvals": pending,  # one line and link each in « À traiter »
        "by_status": (
            submission_counts(user, thesis) if can(user, Action.SUBMISSION_READ, thesis) else []
        ),
    }


# =========================================================================== submissions


def _seal_texts(sealer: Any, submission: Submission, texts: Mapping[str, str]) -> None:
    for name, value in texts.items():
        if value:
            sealer.seal(submission, name, value)
        else:
            sealer.erase(submission, name)


def _sealing(user: User, thesis: Thesis, level: ConfidentialityLevel, action: Action) -> Any:
    return vault.sealing(user=user, thesis=thesis, level=level, action=action)


def create_submission(
    *,
    thesis: Thesis,
    user: User,
    venue: Venue,
    edition: Edition | None,
    title: str,
    level: ConfidentialityLevel,
    abstract: str = "",
    external_authors: str = "",
    notes: str = "",
    objective: Objective | None = None,
    authors: Iterable[User] = (),
    st_work: bool = True,
    resubmission_of: Submission | None = None,
) -> Submission:
    _require(user, Action.SUBMISSION_WRITE, thesis, "create")
    _check_open(thesis)
    _check_target(thesis, venue, edition)
    level = _clean_level(level)
    _check_st_level(st_work, level)
    _check_objective(thesis, objective)
    people = _check_authors(thesis, authors, level)
    texts = {
        "title": _clean_text(title, "title"),
        "abstract": _clean_text(abstract, "abstract"),
        "external_authors": _clean_text(external_authors, "external_authors"),
        "notes": _clean_text(notes, "notes"),
    }
    with _sealing(user, thesis, level, Action.SUBMISSION_WRITE) as sealer:
        _lock_thesis(thesis.pk)
        submission = Submission(
            thesis=thesis,
            venue=venue,
            edition=edition,
            objective=objective,
            level=level.value,
            st_work=st_work,
            resubmission_of=resubmission_of,
            created_by=user,
        )
        submission.save(force_insert=True)
        _seal_texts(sealer, submission, {k: v for k, v in texts.items() if v})
        for person in people:
            SubmissionAuthor.objects.create(submission=submission, user=person, created_by=user)
        details: dict[str, Any] = {
            "level": submission.level,
            "st_work": st_work,
            "venue_kind": venue.kind,
            "authors": len(people),
        }
        activity.record(
            kind="SUBMISSION_CREATED",
            obj=submission,
            object_type="SUBMISSION",
            thesis=thesis,
            actor=user,
        )
        if resubmission_of is not None:
            audit(
                AuditAction.SUBMISSION_RESUBMITTED,
                actor=user,
                obj=submission,
                source_id=str(resubmission_of.pk),
                **details,
            )
        else:
            audit(AuditAction.SUBMISSION_CREATED, actor=user, obj=submission, **details)
    return submission


def _current_texts(user: User, thesis: Thesis, submission: Submission) -> dict[str, Any]:
    opened = vault.open_texts(
        user, thesis, [(submission, n) for n in SUBMISSION_TEXTS], action=Action.SUBMISSION_READ
    )
    texts = {n: opened.get((submission.pk, n), "") for n in SUBMISSION_TEXTS}
    if any(v is vault.RESTRICTED for v in texts.values()):
        raise _deny(user, "update", "unreadable_level", obj=submission)
    return texts


def _approval_on_record(submission: Submission) -> bool:
    """A request of ST's approval for submitting, pending or granted, binds the target."""
    return StApproval.objects.filter(
        submission=submission,
        purpose=ApprovalPurpose.SUBMISSION,
        decision__in=[ApprovalDecision.PENDING, ApprovalDecision.GRANTED],
    ).exists()


def update_submission(
    *, submission: Submission, user: User, changes: Mapping[str, Any]
) -> list[str]:
    """Texts, target (venue, edition) and objective of a live submission."""
    allowed = {*SUBMISSION_TEXTS, "venue", "edition", "objective"}
    unknown = sorted(set(changes) - allowed)
    if unknown:
        raise ValueError(f"{unknown} cannot be changed here")
    thesis = _thesis(submission)
    _require(user, Action.SUBMISSION_WRITE, thesis, "update")
    _check_open(thesis)
    fresh = Submission.objects.get(pk=submission.pk)
    if fresh.is_terminal:
        raise ValidationError(_("This submission is closed."), code="terminal")
    venue = changes.get("venue", fresh.venue)
    edition = changes.get("edition", fresh.edition)
    _check_target(thesis, venue, edition)
    objective = _check_objective(thesis, changes.get("objective", fresh.objective))
    retargeted = venue.pk != fresh.venue_id or (edition.pk if edition else None) != fresh.edition_id
    if retargeted and _approval_on_record(fresh):
        raise ValidationError(
            _(
                "ST's approval was requested for this target: submit elsewhere instead "
                "(a new submission, a new approval)."
            ),
            code="target_locked",
        )
    texts = {n: _clean_text(changes[n], n) for n in SUBMISSION_TEXTS if n in changes}
    current = _current_texts(user, thesis, fresh)
    changed = [n for n, v in texts.items() if v != current[n]]
    if venue.pk != fresh.venue_id:
        changed.append("venue")
    if (edition.pk if edition else None) != fresh.edition_id:
        changed.append("edition")
    if (objective.pk if objective else None) != fresh.objective_id:
        changed.append("objective")
    if not changed:
        return []
    with _sealing(user, thesis, _level(fresh), Action.SUBMISSION_WRITE) as sealer:
        locked = _locked(fresh)
        if locked.updated_at != fresh.updated_at:
            raise ValidationError(_("The submission changed meanwhile: try again."), code="stale")
        _seal_texts(sealer, locked, {n: texts[n] for n in changed if n in texts})
        locked.venue, locked.edition, locked.objective = venue, edition, objective
        locked.save()
        audit(AuditAction.SUBMISSION_UPDATED, actor=user, obj=locked, fields=changed)
    return changed


def set_authors(*, submission: Submission, user: User, authors: Iterable[User]) -> None:
    thesis = _thesis(submission)
    _require(user, Action.SUBMISSION_WRITE, thesis, "authors")
    _check_open(thesis)
    people = _check_authors(thesis, authors, _level(submission))
    with transaction.atomic():
        locked = _locked(submission)
        before = set(
            SubmissionAuthor.objects.filter(submission=locked).values_list("user_id", flat=True)
        )
        after = {p.pk for p in people}
        if before == after:
            return
        SubmissionAuthor.objects.filter(submission=locked, user_id__in=before - after).delete()
        for person in people:
            if person.pk not in before:
                SubmissionAuthor.objects.create(submission=locked, user=person, created_by=user)
        locked.save(update_fields=["updated_at"])
        audit(
            AuditAction.SUBMISSION_AUTHORS_CHANGED,
            actor=user,
            obj=locked,
            added=len(after - before),
            removed=len(before - after),
        )


def change_level(
    *, submission: Submission, user: User, level: ConfidentialityLevel, recent_mfa: bool
) -> Submission:
    """Move every text of the submission (approval comments, preprint URLs too)."""
    thesis = _thesis(submission)
    _require(user, Action.SUBMISSION_WRITE, thesis, "level")
    _require_mfa(recent_mfa)
    level = _clean_level(level)
    _check_open(thesis)
    fresh = Submission.objects.get(pk=submission.pk)
    _check_st_level(fresh.st_work, level)
    author_ids = SubmissionAuthor.objects.filter(submission=fresh).values_list("user_id", flat=True)
    _check_authors(thesis, User.objects.filter(pk__in=list(author_ids)), level)
    previous = _level(fresh)
    if level == previous:
        return fresh
    with _sealing(user, thesis, level, Action.SUBMISSION_WRITE) as sealer:
        locked = _locked(fresh)
        if locked.level != previous.value or locked.st_work != fresh.st_work:
            raise ValidationError(_("The submission changed meanwhile: try again."), code="stale")
        for name in SUBMISSION_TEXTS:
            sealer.reseal(locked, name)
        for approval in StApproval.objects.filter(submission=locked):
            sealer.reseal(approval, "comment")
        for preprint in Preprint.objects.filter(submission=locked):
            sealer.reseal(preprint, "url")
        locked.level = level.value
        locked.save()
        audit(
            AuditAction.SUBMISSION_LEVEL_CHANGED,
            actor=user,
            obj=locked,
            previous=previous.value,
            level=level.value,
        )
    return locked


def set_st_work(*, submission: Submission, user: User, value: bool, recent_mfa: bool) -> Submission:
    """``st_work`` off: the tutor only, with a second factor (it removes the ST
    approval step); on: the student or the tutor, any time."""
    thesis = _thesis(submission)
    if value:
        if not (
            can(user, Action.SUBMISSION_WRITE, thesis)
            or can(user, Action.VALIDATE_PUBLICATION, thesis)
        ):
            raise _deny(user, "st_work", "missing_right", obj=submission)
    else:
        _require(user, Action.VALIDATE_PUBLICATION, thesis, "st_work")
        _require_mfa(recent_mfa)
    _check_open(thesis)
    with transaction.atomic():
        locked = _locked(submission)
        if locked.st_work == value:
            return locked
        if value:
            _check_st_level(True, _level(locked))
        elif StApproval.objects.filter(
            submission=locked, decision=ApprovalDecision.PENDING
        ).exists():
            raise ValidationError(
                _("An ST approval is pending: decide it first."), code="approval_pending"
            )
        locked.st_work = value
        locked.save(update_fields=["st_work", "updated_at"])
        audit(AuditAction.SUBMISSION_ST_WORK_CHANGED, actor=user, obj=locked, st_work=value)
    return locked


# =========================================================================== documents


def link_document(*, submission: Submission, user: User, document: Document) -> SubmissionDocument:
    thesis = _thesis(submission)
    _require(user, Action.SUBMISSION_WRITE, thesis, "link_document")
    _check_open(thesis)
    if document.thesis_id != thesis.pk:
        raise _deny(user, "link_document", "other_thesis", obj=submission)
    if not can_access_document(user, document):
        raise _deny(user, "link_document", "unreadable_document", obj=submission)
    with transaction.atomic():
        locked = _locked(submission)
        if SubmissionDocument.objects.filter(submission=locked, document=document).exists():
            raise ValidationError(_("This document is already linked."), code="already_linked")
        link = SubmissionDocument.objects.create(
            submission=locked, document=document, created_by=user
        )
        activity.record(
            kind="DOCUMENT_LINKED",
            obj=link,
            object_type="SUBMISSION_DOCUMENT",
            thesis=thesis,
            actor=user,
        )
        audit(
            AuditAction.SUBMISSION_DOCUMENT_LINKED,
            actor=user,
            obj=locked,
            document_id=str(document.pk),
        )
    return link


def unlink_document(*, link: SubmissionDocument, user: User) -> None:
    submission = Submission.objects.get(pk=link.submission_id)
    thesis = _thesis(submission)
    _require(user, Action.SUBMISSION_WRITE, thesis, "unlink_document")
    _check_open(thesis)
    with transaction.atomic():
        locked = _locked(submission)
        row = SubmissionDocument.objects.select_for_update().filter(pk=link.pk).first()
        if row is None:
            raise ValidationError(_("This document is no longer linked."), code="stale")
        document_id = str(row.document_id)
        row.delete()
        audit(
            AuditAction.SUBMISSION_DOCUMENT_UNLINKED,
            actor=user,
            obj=locked,
            document_id=document_id,
        )


def _check_version(submission: Submission, version: DocumentVersion | None) -> DocumentVersion:
    if (
        not isinstance(version, DocumentVersion)
        or not SubmissionDocument.objects.filter(
            submission=submission, document_id=version.document_id
        ).exists()
    ):
        raise ValidationError(
            _("Choose a version of a document linked to this submission."), code="version"
        )
    return version


def _is_current(version: DocumentVersion) -> bool:
    return version.version == Document.objects.get(pk=version.document_id).current_version


def _granted(submission: Submission, version: DocumentVersion) -> bool:
    """A GRANTED approval of this exact version, for THIS submission."""
    return StApproval.objects.filter(
        submission=submission, version=version, decision=ApprovalDecision.GRANTED
    ).exists()


def _missing_approval() -> ValidationError:
    return ValidationError(
        _("ST's approval of this exact version is required first."), code="missing_approval"
    )


# =========================================================================== ST approval


def reference_deadline(submission: Submission, kind: str) -> Deadline | None:
    """The next deadline of ``kind`` of the edition (the last one once all passed)."""
    if submission.edition_id is None:
        return None
    rows = Deadline.objects.filter(
        edition_id=submission.edition_id, kind=kind, cancelled_at__isnull=True
    )
    upcoming = rows.filter(due_at__gte=timezone.now()).order_by("due_at").first()
    return upcoming or rows.order_by("-due_at").first()


def _request_due(submission: Submission, purpose: str, days: int) -> dt.date | None:
    """Paris date of the reference deadline - the review period (a date, never hours)."""
    kind = REFERENCE_DEADLINE.get(ApprovalPurpose(purpose))
    deadline = reference_deadline(submission, kind) if kind else None
    if deadline is None:
        return None
    return timezone.localtime(deadline.due_at).date() - dt.timedelta(days=days)


def _resting_status(submission: Submission) -> SubmissionStatus:
    """PREPARING or ST_GRANTED, from the approvals of the submission (purpose SUBMISSION)."""
    for approval in StApproval.objects.filter(
        submission=submission,
        purpose=ApprovalPurpose.SUBMISSION,
        decision=ApprovalDecision.GRANTED,
    ).select_related("version"):
        if _is_current(approval.version):
            return S.ST_GRANTED
    return S.PREPARING


def request_approval(
    *,
    submission: Submission,
    user: User,
    version: DocumentVersion,
    purpose: str = ApprovalPurpose.SUBMISSION,
) -> StApproval:
    """The student asks ST's approval of an exact version, for one purpose."""
    thesis = _thesis(submission)
    _require(user, Action.SUBMISSION_WRITE, thesis, "request_approval")
    _check_open(thesis)
    if purpose not in ApprovalPurpose.values:
        raise ValidationError(_("Unknown purpose."), code="purpose")
    fresh = Submission.objects.get(pk=submission.pk)
    if not fresh.st_work:
        raise ValidationError(
            _("This submission does not concern ST work: no approval is needed."),
            code="not_st_work",
        )
    _check_version(fresh, version)
    tutors = members_with_role(thesis, Role.INDUSTRIAL_TUTOR)
    if not tutors:
        raise ValidationError(
            _("No industrial tutor in the team: invite them first."), code="no_tutor"
        )
    document = Document.objects.get(pk=version.document_id)
    if not any(can_access_document(tutor, document) for tutor in tutors):
        raise ValidationError(
            _("The industrial tutor cannot read this document: change its level first."),
            code="tutor_cannot_read",
        )
    days = review_days(thesis)
    with transaction.atomic():
        locked = _locked(fresh)
        pending = StApproval.objects.filter(
            submission=locked, purpose=purpose, decision=ApprovalDecision.PENDING
        ).exists()
        if pending:
            raise ValidationError(_("A request is already pending."), code="already_pending")
        allowed = {
            ApprovalPurpose.SUBMISSION: locked.status in (S.PREPARING, S.ST_GRANTED),
            ApprovalPurpose.CAMERA_READY: locked.status == S.ACCEPTED,
            ApprovalPurpose.PREPRINT: not locked.is_terminal,
        }[ApprovalPurpose(purpose)]
        if not allowed:
            raise _transition()
        approval = StApproval.objects.create(
            thesis=thesis,
            submission=locked,
            purpose=purpose,
            version=version,
            review_days=days,
            request_due=_request_due(locked, purpose, days),
            requested_by=user,
            created_by=user,
        )
        if purpose == ApprovalPurpose.SUBMISSION:
            locked.status = S.ST_REQUESTED
            locked.save(update_fields=["status", "updated_at"])
        notify(thesis, NotificationKind.APPROVAL_REQUESTED, approval.pk, tutors, exclude=user)
        activity.record(
            kind="APPROVAL_REQUESTED",
            obj=approval,
            object_type="APPROVAL",
            thesis=thesis,
            actor=user,
        )
        audit(
            AuditAction.ST_APPROVAL_REQUESTED,
            actor=user,
            obj=locked,
            approval_id=str(approval.pk),
            purpose=purpose,
            version_id=str(version.pk),
            review_days=days,
            request_due=approval.request_due.isoformat() if approval.request_due else None,
        )
    return approval


def decide_approval(
    *,
    approval: StApproval,
    user: User,
    decision: str,
    reason: str = "",
    comment: str = "",
    recent_mfa: bool,
) -> StApproval:
    """The industrial tutor grants or refuses (reason from a closed list, optional
    sealed comment), with a second factor less than 10 minutes old."""
    thesis = _thesis(approval)
    _require(user, Action.VALIDATE_PUBLICATION, thesis, "decide_approval")
    _require_mfa(recent_mfa)
    if decision not in (ApprovalDecision.GRANTED, ApprovalDecision.REFUSED):
        raise ValidationError(_("Grant or refuse."), code="decision")
    if decision == ApprovalDecision.REFUSED and reason not in RefusalReason.values:
        raise ValidationError(_("A refusal needs its reason."), code="reason")
    if decision == ApprovalDecision.GRANTED and reason:
        raise ValidationError(_("A grant has no refusal reason."), code="reason")
    text = _clean_text(comment, "comment")
    _check_open(thesis)
    submission = Submission.objects.get(pk=approval.submission_id)
    with _sealing(user, thesis, _level(submission), Action.VALIDATE_PUBLICATION) as sealer:
        locked_submission = _locked(submission)
        locked = StApproval.objects.select_for_update().get(pk=approval.pk)
        if locked.decision != ApprovalDecision.PENDING:
            raise ValidationError(_("This request was already decided."), code="decided")
        if locked_submission.level != submission.level:
            raise ValidationError(_("The submission changed meanwhile: try again."), code="stale")
        if text:
            sealer.seal(locked, "comment", text)
        locked.decision, locked.reason = decision, reason
        locked.decided_by, locked.decided_at = user, timezone.now()
        locked.save()
        if locked.purpose == ApprovalPurpose.SUBMISSION:
            locked_submission.status = (
                S.ST_GRANTED
                if decision == ApprovalDecision.GRANTED
                else _resting_status(locked_submission)
            )
            locked_submission.save(update_fields=["status", "updated_at"])
        notify(thesis, NotificationKind.APPROVAL_DECIDED, locked.pk, [locked.requested_by])
        details: dict[str, Any] = {
            "approval_id": str(locked.pk),
            "purpose": locked.purpose,
            "version_id": str(locked.version_id),
        }
        activity.record(
            kind="APPROVAL_DECIDED",
            obj=locked,
            object_type="APPROVAL",
            thesis=thesis,
            actor=user,
        )
        if decision == ApprovalDecision.GRANTED:
            audit(AuditAction.ST_APPROVAL_GRANTED, actor=user, obj=locked_submission, **details)
        else:
            audit(
                AuditAction.ST_APPROVAL_REFUSED,
                actor=user,
                obj=locked_submission,
                reason=reason,
                **details,
            )
    return locked


def withdraw_approval(*, approval: StApproval, user: User) -> StApproval:
    thesis = _thesis(approval)
    _require(user, Action.SUBMISSION_WRITE, thesis, "withdraw_approval")
    _check_open(thesis)
    with transaction.atomic():
        submission = _locked(Submission.objects.get(pk=approval.submission_id))
        locked = StApproval.objects.select_for_update().get(pk=approval.pk)
        if locked.decision != ApprovalDecision.PENDING:
            raise ValidationError(_("This request was already decided."), code="decided")
        _withdraw(locked)
        if locked.purpose == ApprovalPurpose.SUBMISSION:
            submission.status = _resting_status(submission)
            submission.save(update_fields=["status", "updated_at"])
        audit(
            AuditAction.ST_APPROVAL_WITHDRAWN,
            actor=user,
            obj=submission,
            approval_id=str(locked.pk),
        )
    return locked


def _withdraw(approval: StApproval) -> None:
    approval.decision = ApprovalDecision.WITHDRAWN
    approval.decided_at = timezone.now()
    approval.save()


# =========================================================================== status


def set_status(
    *, submission: Submission, user: User, status: str, version: DocumentVersion | None = None
) -> Submission:
    """The student moves the submission along; SUBMITTED and PUBLISHED name a
    version and, for ST work, need ST's approval of exactly that version."""
    thesis = _thesis(submission)
    _require(user, Action.SUBMISSION_WRITE, thesis, "status")
    _check_open(thesis)
    with transaction.atomic():
        locked = _locked(submission)
        current = SubmissionStatus(locked.status)
        if status not in S.values:
            raise _transition()
        target = SubmissionStatus(status)
        now = timezone.now()
        if target == S.SUBMITTED:
            if current not in (S.ST_GRANTED, S.PREPARING):
                raise _transition()
            if locked.st_work:
                checked = _check_version(locked, version)
                if not _granted(locked, checked):
                    raise _missing_approval()
                if not _is_current(checked):
                    raise ValidationError(
                        _("The document has a newer version than the one ST approved."),
                        code="approval_outdated",
                    )
            elif version is not None:
                _check_version(locked, version)
            locked.submitted_version, locked.submitted_at = version, now
        elif target in (S.ACCEPTED, S.REJECTED):
            if current != S.SUBMITTED:
                raise _transition()
        elif target == S.PUBLISHED:
            if current != S.ACCEPTED:
                raise _transition()
            if locked.st_work:
                checked = _check_version(locked, version)
                if not _granted(locked, checked):
                    raise _missing_approval()
            elif version is not None:
                _check_version(locked, version)
            locked.published_version, locked.published_at = version, now
        elif target == S.WITHDRAWN:
            if locked.is_terminal:
                raise _transition()
            for pending in StApproval.objects.select_for_update().filter(
                submission=locked, decision=ApprovalDecision.PENDING
            ):
                _withdraw(pending)
        else:
            raise _transition()
        locked.status = target
        locked.save()
        activity.record(
            kind="SUBMISSION_STATUS_CHANGED",
            obj=locked,
            object_type="SUBMISSION",
            thesis=thesis,
            actor=user,
        )
        audit(
            AuditAction.SUBMISSION_STATUS_CHANGED,
            actor=user,
            obj=locked,
            **_from_to(current.value, target.value),
            version_id=str(version.pk) if version is not None else None,
        )
    return locked


# =========================================================================== preprint


def add_preprint(
    *,
    submission: Submission,
    user: User,
    platform: str,
    posted_on: dt.date,
    url: str,
    version: DocumentVersion | None = None,
) -> Preprint:
    """A public preprint (ePrint, arXiv, HAL, other): for ST work, ST's approval of
    the exact version posted. Its URL is sealed at the submission's level."""
    thesis = _thesis(submission)
    _require(user, Action.SUBMISSION_WRITE, thesis, "preprint")
    _check_open(thesis)
    if platform not in PreprintPlatform.values:
        raise ValidationError(_("Unknown platform."), code="platform")
    link = _clean_url(url, code="url", limit="preprint_url")
    if (
        isinstance(posted_on, dt.datetime)
        or not isinstance(posted_on, dt.date)
        or posted_on > timezone.localdate()
    ):
        raise ValidationError(_("The date of a preprint is today or earlier."), code="posted_on")
    fresh = Submission.objects.get(pk=submission.pk)
    if fresh.is_terminal:
        raise ValidationError(_("This submission is closed."), code="terminal")
    if fresh.st_work:
        checked = _check_version(fresh, version)
        if not _granted(fresh, checked):
            raise _missing_approval()
    elif version is not None:
        _check_version(fresh, version)
    with _sealing(user, thesis, _level(fresh), Action.SUBMISSION_WRITE) as sealer:
        locked = _locked(fresh)
        if locked.is_terminal or locked.level != fresh.level or locked.st_work != fresh.st_work:
            raise ValidationError(_("The submission changed meanwhile: try again."), code="stale")
        preprint = Preprint(
            thesis=thesis, submission=locked, platform=platform, posted_on=posted_on,
            version=version, created_by=user,
        )  # fmt: skip
        preprint.save(force_insert=True)
        sealer.seal(preprint, "url", link)
        details: dict[str, Any] = {"platform": platform, "posted_on": posted_on.isoformat()}
        if version is not None:
            details["version_id"] = str(version.pk)
        audit(AuditAction.SUBMISSION_PREPRINTED, actor=user, obj=locked, **details)
    return preprint


# =========================================================================== resubmit


def resubmit_elsewhere(
    *, submission: Submission, user: User, venue: Venue, edition: Edition | None
) -> Submission:
    """A new submission from a rejected or withdrawn one: same texts, co-authors,
    documents, level, st_work and objective; never its approvals."""
    thesis = _thesis(submission)
    _require(user, Action.SUBMISSION_WRITE, thesis, "resubmit")
    source = Submission.objects.get(pk=submission.pk)
    if source.status not in (S.REJECTED, S.WITHDRAWN):
        raise _transition()
    texts = _current_texts(user, thesis, source)
    author_ids = SubmissionAuthor.objects.filter(submission=source).values_list(
        "user_id", flat=True
    )
    from apps.meetings.members import internal_member_ids

    members = internal_member_ids(thesis.pk)
    authors = User.objects.filter(pk__in=[pk for pk in author_ids if pk in members])
    copy = create_submission(
        thesis=thesis,
        user=user,
        venue=venue,
        edition=edition,
        level=_level(source),
        objective=source.objective,
        authors=list(authors),
        st_work=source.st_work,
        resubmission_of=source,
        **{name: str(value) for name, value in texts.items()},
    )
    for link in SubmissionDocument.objects.filter(submission=source).select_related("document"):
        if can_access_document(user, link.document):
            link_document(submission=copy, user=user, document=link.document)
    return copy
