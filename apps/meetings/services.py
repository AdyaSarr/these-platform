"""Business logic of the meetings (M5a: internal members only).

Rights are decided HERE (accounts.permissions), even when a view checked too:
- read meetings, the decision register, linked actions: MEETING_READ AND the
  meeting's level (SR-01: « on ne voit l'existence que de ce qu'on peut lire »):
  a meeting of another level is neither listed, counted nor reachable, and its
  page answers as an unknown id (Meeting.objects.readable_by);
- write (edit, agenda, minutes, decisions, actions, documents, cancel,
  duplicate): MEETING_WRITE AND (author or attendee); the vault also requires
  read access to the meeting's level;
- mark the minutes read / approved: MEETING_ACKNOWLEDGE AND attendee; approving
  is a validation: second factor less than 10 minutes old (``recent_mfa``);
- change the level: a writer, with a recent second factor;
- change an action's status: its assignee or a writer.
A refusal is audited (MEETING_ACCESS_DENIED) in its own transaction, then
PermissionDenied is raised.

Every text goes through the vault at the meeting's level; the audit log gets
codes, counts and ids, never a text. Lock order (CLAUDE.md): KEK lock (opened by
vault.sealing()), thesis row, meeting row, then audit() LAST. Current texts are
read BEFORE the sealing block (open_texts audits a CONFIDENTIAL read); a
concurrent change in between is detected (``stale``).

Minutes, decisions, actions and acknowledgements open when the meeting starts
(``starts_at <= now``, server time: ``not_started`` before); the agenda is
prepared before.

M5a: a CSI meeting is planned (frame, attendees, agenda, documents) but its
minutes, decisions, actions and acknowledgements wait for M5b (``csi_pending``):
nothing is stored that cannot yet be protected correctly.
"""

from __future__ import annotations

import datetime as dt
import unicodedata
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from django.core.exceptions import PermissionDenied, ValidationError
from django.core.validators import URLValidator
from django.db import transaction
from django.db.models import Q, QuerySet
from django.utils import timezone
from django.utils.translation import gettext as _

from apps.accounts.models import Thesis, User
from apps.accounts.permissions import (
    LEVEL_ROLES,
    Action,
    can,
    can_access_document,
    roles_of,
    sees,
)
from apps.accounts.statuses import open_statuses
from apps.activity import events as activity
from apps.core.confidentiality import ConfidentialityLevel
from apps.objectives.models import Objective
from apps.objectives.models import Status as ObjectiveStatus
from apps.vault import formats
from apps.vault import services as vault
from apps.vault.audit import AuditAction, audit
from apps.vault.errors import ReauthenticationRequiredError
from apps.vault.models import Document

from . import timing
from .members import INTERNAL_ROLES, internal_member_ids
from .models import (
    ActionItem,
    Attendee,
    Decision,
    Meeting,
    MeetingDocument,
    MinutesAcknowledgement,
)
from .notifications import Kind as Notify
from .notifications import notify

OPEN_THESIS = open_statuses("meetings")  # apps/accounts/statuses.py
MEETING_TEXTS = ("title", "agenda", "minutes", "location_detail", "visio_link")
FRAME_FIELDS = ("kind", "presentation_venue", "starts_at", "duration_minutes", "location")
EDITABLE_TEXTS = ("title", "location_detail", "visio_link")
# A change of these moves the meeting in the attendees' calendars (.ics SEQUENCE).
CALENDAR_FIELDS = frozenset({"kind", "starts_at", "duration_minutes", "location"})
NOTIFIED_FIELDS = frozenset({"starts_at", "duration_minutes", "location"})
TEXT_LIMITS = {
    "title": (1, 200),
    "agenda": (0, 8000),
    "minutes": (1, 16000),
    "location_detail": (0, 200),
    "visio_link": (0, 500),
    "decision": (1, 2000),
    "action": (1, 300),
}
_ALLOWED_CONTROLS = frozenset("\n\t")
_https = URLValidator(schemes=["https"])
Ack = MinutesAcknowledgement.Kind


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


def _clean_link(value: Any) -> str:
    link = _clean_text(value, "visio_link")
    if link:
        try:
            _https(link)
        except ValidationError:
            raise ValidationError(
                _("The video link must be an https:// address."), code="visio_link"
            ) from None
    return link


def _clean_named_text(name: str, value: Any) -> str:
    return _clean_link(value) if name == "visio_link" else _clean_text(value, name)


def _clean_level(level: Any) -> ConfidentialityLevel:
    if not isinstance(level, ConfidentialityLevel):
        raise ValidationError(_("Unknown confidentiality level."), code="level")
    return level


def _clean_frame(thesis: Thesis, frame: Mapping[str, Any]) -> dict[str, Any]:
    kind, venue = frame["kind"], frame["presentation_venue"] or ""
    if kind not in Meeting.Kind.values:
        raise ValidationError(_("Unknown meeting type."), code="kind")
    if kind == Meeting.Kind.PRESENTATION:
        if venue not in Meeting.Venue.values:
            raise ValidationError(
                _("A presentation needs its venue (seminar, ST meeting, other)."),
                code="presentation_venue",
            )
    else:
        venue = ""
    if frame["location"] not in Meeting.Location.values:
        raise ValidationError(_("Unknown place."), code="location")
    duration = frame["duration_minutes"]
    if type(duration) is not int or not 5 <= duration <= 600:
        raise ValidationError(_("The duration is between 5 and 600 minutes."), code="duration")
    starts_at = frame["starts_at"]
    if not isinstance(starts_at, dt.datetime) or timezone.is_naive(starts_at):
        raise ValidationError(_("A date and time are required."), code="starts_at")
    timing.check_date(
        timezone.localtime(starts_at).date(), thesis.cifre_start_date, thesis.planned_end_date
    )
    return {
        "kind": kind,
        "presentation_venue": venue,
        "starts_at": starts_at,
        "duration_minutes": duration,
        "location": frame["location"],
    }


def _check_attendees(
    thesis: Thesis, users: Iterable[User], level: ConfidentialityLevel
) -> list[User]:
    """Active internal members of the thesis, each able to read ``level``."""
    people = list({u.pk: u for u in users}.values())
    if not people:
        raise ValidationError(_("A meeting needs at least one attendee."), code="attendees")
    for user in people:
        roles = roles_of(user, thesis) & INTERNAL_ROLES
        if not roles:
            raise ValidationError(
                _("An attendee must be an active member of the supervision team."),
                code="attendee_not_member",
            )
        if not roles & LEVEL_ROLES[level]:
            raise ValidationError(
                _("An attendee could not read this confidentiality level."),
                code="attendee_level",
            )
    return people


def _check_assignee(thesis: Thesis, user: Any) -> User:
    if not isinstance(user, User) or not roles_of(user, thesis) & INTERNAL_ROLES:
        raise ValidationError(
            _("The person in charge must be an active member of the supervision team."),
            code="assignee",
        )
    return user


def _check_objective(thesis: Thesis, objective: Objective | None) -> Objective | None:
    if objective is not None and objective.thesis_id != thesis.pk:
        raise ValidationError(_("This objective belongs to another thesis."), code="objective")
    return objective


def _check_due(thesis: Thesis, due: Any) -> dt.date:
    if isinstance(due, dt.datetime) or not isinstance(due, dt.date):
        raise ValidationError(_("A date is required."), code="due_date")
    timing.check_date(due, thesis.cifre_start_date, thesis.planned_end_date)
    return due


# =========================================================================== rights


def _deny(user: Any, operation: str, reason: str, **kwargs: Any) -> PermissionDenied:
    """MEETING_ACCESS_DENIED in its own transaction (kept although an exception follows)."""
    with transaction.atomic():
        audit(
            AuditAction.MEETING_ACCESS_DENIED,
            actor=user,
            operation=operation,
            reason=reason,
            **kwargs,
        )
    return PermissionDenied()


def _require(user: Any, action: Action, thesis: Thesis, operation: str) -> None:
    if not can(user, action, thesis):
        raise _deny(user, operation, "missing_right", thesis=thesis)


def _thesis(obj: Meeting | Decision | ActionItem | Objective) -> Thesis:
    return Thesis.objects.get(pk=obj.thesis_id)


def is_attendee(user: Any, meeting: Meeting) -> bool:
    return bool(user.pk) and Attendee.objects.filter(meeting=meeting, user_id=user.pk).exists()


def is_writer(user: Any, meeting: Meeting) -> bool:
    """MEETING_WRITE AND (author or attendee). The level is checked by the vault."""
    return can(user, Action.MEETING_WRITE, meeting) and (
        meeting.created_by_id == user.pk or is_attendee(user, meeting)
    )


def _require_writer(user: Any, meeting: Meeting, operation: str) -> Thesis:
    thesis = _thesis(meeting)
    _require(user, Action.MEETING_WRITE, thesis, operation)
    if not is_writer(user, meeting):
        raise _deny(user, operation, "not_attendee", obj=meeting)
    return thesis


def require_writer(user: Any, meeting: Meeting, operation: str) -> None:
    """For the views' per-meeting guard (before any re-authentication)."""
    _require_writer(user, meeting, operation)


def refuse_unknown(user: Any, meeting: Meeting, operation: str, what: str) -> PermissionDenied:
    """A decision, action or link id that is not of the meeting of the URL: audited."""
    return _deny(user, operation, f"unknown_{what}", obj=meeting)


def require_attendee(user: Any, meeting: Meeting, operation: str) -> None:
    """For the views' per-meeting guard (before any re-authentication)."""
    _require(user, Action.MEETING_ACKNOWLEDGE, _thesis(meeting), operation)
    if not is_attendee(user, meeting):
        raise _deny(user, operation, "not_attendee", obj=meeting)


def _frozen() -> ValidationError:
    return ValidationError(_("The thesis is finished: its meetings are frozen."), code="frozen")


def _check_open(thesis: Thesis) -> None:
    if Thesis.objects.get(pk=thesis.pk).status not in OPEN_THESIS:
        raise _frozen()


def _locked(meeting: Meeting, *, allow_cancelled: bool = False) -> Meeting:
    """Lock the thesis row, THEN the meeting row; refuse a finished thesis or a
    cancelled meeting."""
    if Thesis.objects.select_for_update().get(pk=meeting.thesis_id).status not in OPEN_THESIS:
        raise _frozen()
    locked = Meeting.objects.select_for_update().get(pk=meeting.pk)
    if locked.is_cancelled and not allow_cancelled:
        raise ValidationError(_("This meeting was cancelled."), code="cancelled")
    return locked


def _not_cancelled(meeting: Meeting) -> None:
    if meeting.is_cancelled:
        raise ValidationError(_("This meeting was cancelled."), code="cancelled")


def _not_csi(meeting: Meeting) -> None:
    if meeting.is_csi:
        raise ValidationError(
            _("The minutes of a CSI meeting will be available in a later version."),
            code="csi_pending",
        )


def _started(meeting: Meeting) -> None:
    """Minutes, decisions, actions and marks come from a meeting that has begun
    (server time); the agenda is prepared before."""
    if meeting.starts_at > timezone.now():
        raise ValidationError(
            _("The minutes, decisions and actions open when the meeting starts."),
            code="not_started",
        )


def _stale() -> ValidationError:
    return ValidationError(_("The meeting changed meanwhile: try again."), code="stale")


def _level(meeting: Meeting) -> ConfidentialityLevel:
    return ConfidentialityLevel(meeting.level)


def _sealing(user: User, thesis: Thesis, level: ConfidentialityLevel) -> Any:
    return vault.sealing(user=user, thesis=thesis, level=level, action=Action.MEETING_WRITE)


def _attendee_users(meeting: Meeting) -> list[User]:
    """Every attendance row's user, current member or not (history)."""
    ids = Attendee.objects.filter(meeting=meeting).values_list("user_id", flat=True)
    return list(User.objects.filter(pk__in=list(ids)).order_by("last_name", "email"))


def _current_attendees(meeting: Meeting) -> list[User]:
    """The attendees who are still active internal members of the thesis: the only
    ones who count (level check, duplicate, notifications, attendee edits)."""
    members = internal_member_ids(meeting.thesis_id)
    return [u for u in _attendee_users(meeting) if u.pk in members]


# =========================================================================== reading


def meeting_for(user: User, thesis: Thesis, meeting_id: Any, *, operation: str) -> Meeting:
    """The meeting ``meeting_id`` of ``thesis`` if ``user`` may read meetings.

    Unknown, malformed, or of another thesis than the URL's: the same audited
    PermissionDenied (IDOR: an id never crosses theses).
    """
    try:
        meeting = Meeting.objects.filter(pk=meeting_id, thesis=thesis).first()
    except (ValueError, ValidationError):
        meeting = None
    if meeting is None:
        raise _deny(user, operation, "unknown_meeting", thesis=thesis)
    _require(user, Action.MEETING_READ, thesis, operation)
    _require_level(user, meeting, operation)
    return meeting


def _require_level(user: Any, meeting: Meeting, operation: str) -> None:
    """SR-01: a meeting of a level ``user`` does not read does not exist for them:
    the same PermissionDenied as an unknown id (the journal says why)."""
    if not sees(user, meeting, Action.MEETING_READ):
        raise _deny(user, operation, "unreadable_level", thesis=_thesis(meeting))


def _readable_meetings(user: Any, thesis: Thesis) -> QuerySet[Meeting]:
    return Meeting.objects.readable_by(user, thesis)


@dataclass(frozen=True)
class DecisionView:
    decision: Decision
    text: Any  # str or vault.RESTRICTED


@dataclass(frozen=True)
class ActionView:
    action: ActionItem
    title: Any
    overdue: bool
    objective_shown: bool  # its linked objective, if any, is readable (SR-01)


@dataclass(frozen=True)
class MeetingDetail:
    meeting: Meeting
    texts: dict[str, Any]  # every MEETING_TEXTS name: str ("" if none) or RESTRICTED
    attendees: list[User]  # current members
    former_attendees: list[User]  # attended, no longer a member (kept for the history)
    decisions: list[DecisionView]
    actions: list[ActionView]
    documents: list[Document]
    document_links: dict[uuid.UUID, MeetingDocument]  # by document id, shown ones only
    marks: dict[Any, str]  # user pk -> READ / APPROVED, on the current version
    older_marks: dict[Any, str]  # user pk -> mark on an older version only
    is_attendee: bool
    can_write: bool
    can_acknowledge: bool


def _marks(meeting: Meeting) -> tuple[dict[Any, str], dict[Any, str]]:
    current: dict[Any, str] = {}
    older: dict[Any, tuple[int, str]] = {}
    rows = MinutesAcknowledgement.objects.filter(meeting=meeting).order_by(
        "minutes_version", "created_at"
    )
    for row in rows:
        if row.minutes_version == meeting.minutes_version:
            if current.get(row.user_id) != Ack.APPROVED:
                current[row.user_id] = row.kind
        else:
            version, kind = older.get(row.user_id, (0, ""))
            if row.minutes_version > version or kind != Ack.APPROVED:
                older[row.user_id] = (row.minutes_version, row.kind)
    return current, {pk: kind for pk, (_v, kind) in older.items() if pk not in current}


def _overdue(action: ActionItem, today: dt.date) -> bool:
    return action.status == ActionItem.Status.OPEN and action.due_date < today


def meeting_detail(user: User, meeting: Meeting) -> MeetingDetail:
    """Everything the detail page shows, its texts opened in ONE vault call (one
    SEALED_TEXT_READ per request for a CONFIDENTIAL meeting)."""
    thesis = _thesis(meeting)
    _require(user, Action.MEETING_READ, thesis, "detail")
    meeting = Meeting.objects.get(pk=meeting.pk)
    _require_level(user, meeting, "detail")
    decisions = list(Decision.objects.filter(meeting=meeting).order_by("created_at", "pk"))
    actions = list(
        ActionItem.objects.filter(meeting=meeting)
        .select_related("assignee", "objective")
        .order_by("due_date", "created_at", "pk")
    )
    items: list[tuple[Any, str]] = [(meeting, name) for name in MEETING_TEXTS]
    items += [(d, "text") for d in decisions]
    items += [(a, "title") for a in actions]
    opened = vault.open_texts(user, thesis, items, action=Action.MEETING_READ)
    links = list(
        MeetingDocument.objects.filter(meeting=meeting)
        .select_related("document")
        .order_by("created_at", "pk")
    )
    shown = [link for link in links if can_access_document(user, link.document)]
    marks, older = _marks(meeting)
    goals = set(
        Objective.objects.readable_by(user, thesis)
        .filter(pk__in=[a.objective_id for a in actions if a.objective_id])
        .values_list("pk", flat=True)
    )
    attendee = is_attendee(user, meeting)
    everyone, current = _attendee_users(meeting), _current_attendees(meeting)
    today = timezone.localdate()
    return MeetingDetail(
        meeting=meeting,
        texts={name: opened.get((meeting.pk, name), "") for name in MEETING_TEXTS},
        attendees=current,
        former_attendees=[u for u in everyone if u.pk not in {c.pk for c in current}],
        decisions=[DecisionView(d, opened.get((d.pk, "text"), "")) for d in decisions],
        actions=[
            ActionView(
                a, opened.get((a.pk, "title"), ""), _overdue(a, today), a.objective_id in goals
            )
            for a in actions
        ],
        documents=[link.document for link in shown],
        document_links={link.document_id: link for link in shown},
        marks=marks,
        older_marks=older,
        is_attendee=attendee,
        can_write=is_writer(user, meeting),
        can_acknowledge=attendee and can(user, Action.MEETING_ACKNOWLEDGE, thesis),
    )


@dataclass(frozen=True)
class MeetingRow:
    meeting: Meeting
    title: Any
    is_attendee: bool


def _start_of_today() -> dt.datetime:
    today = timezone.localdate()
    return timezone.make_aware(dt.datetime.combine(today, dt.time()))


def meeting_list(
    user: User, thesis: Thesis, *, past: bool, kind: str | None = None
) -> list[MeetingRow]:
    """Upcoming (from today in Paris) or past meetings; titles in one vault call."""
    _require(user, Action.MEETING_READ, thesis, "list")
    rows = _readable_meetings(user, thesis)
    start = _start_of_today()
    rows = (
        rows.filter(starts_at__lt=start).order_by("-starts_at", "pk")
        if past
        else rows.filter(starts_at__gte=start).order_by("starts_at", "pk")
    )
    if kind:
        rows = rows.filter(kind=kind)
    meetings = list(rows)
    mine = set(
        Attendee.objects.filter(meeting__in=meetings, user_id=user.pk).values_list(
            "meeting_id", flat=True
        )
    )
    opened = vault.open_texts(
        user, thesis, [(m, "title") for m in meetings], action=Action.MEETING_READ
    )
    return [MeetingRow(m, opened.get((m.pk, "title"), ""), m.pk in mine) for m in meetings]


@dataclass(frozen=True)
class RegisterRow:
    decision: Decision
    meeting: Meeting
    text: Any
    meeting_title: Any


def _paris_range(start: dt.date | None, end: dt.date | None) -> Q:
    query = Q()
    if start is not None:
        query &= Q(
            meeting__starts_at__gte=timezone.make_aware(dt.datetime.combine(start, dt.time()))
        )
    if end is not None:
        after = end + dt.timedelta(days=1)
        query &= Q(
            meeting__starts_at__lt=timezone.make_aware(dt.datetime.combine(after, dt.time()))
        )
    return query


def decision_register(
    user: User,
    thesis: Thesis,
    *,
    kind: str | None = None,
    start: dt.date | None = None,
    end: dt.date | None = None,
    withdrawn: bool = False,
) -> list[RegisterRow]:
    """Decisions of every meeting, filtered by meeting type and dates (in Paris);
    withdrawn ones only on request. Texts and meeting titles in one vault call."""
    _require(user, Action.MEETING_READ, thesis, "register")
    rows = (
        Decision.objects.filter(thesis=thesis, meeting__in=_readable_meetings(user, thesis))
        .select_related("meeting")
        .filter(_paris_range(start, end))
    )
    if kind:
        rows = rows.filter(meeting__kind=kind)
    if not withdrawn:
        rows = rows.filter(withdrawn_at__isnull=True)
    decisions = list(rows.order_by("meeting__starts_at", "created_at", "pk"))
    meetings = {d.meeting_id: d.meeting for d in decisions}
    items: list[tuple[Any, str]] = [(d, "text") for d in decisions]
    items += [(m, "title") for m in meetings.values()]
    opened = vault.open_texts(user, thesis, items, action=Action.MEETING_READ)
    return [
        RegisterRow(
            d, d.meeting, opened.get((d.pk, "text"), ""), opened.get((d.meeting_id, "title"), "")
        )
        for d in decisions
    ]


def overdue_actions(thesis: Thesis) -> QuerySet[ActionItem]:
    """OPEN actions due before today's date in Paris."""
    return ActionItem.objects.filter(
        thesis=thesis, status=ActionItem.Status.OPEN, due_date__lt=timezone.localdate()
    ).order_by("due_date", "pk")


@dataclass(frozen=True)
class LinkedAction:
    action: ActionItem
    meeting: Meeting
    title: Any
    overdue: bool


def actions_for_objective(user: User, objective: Objective) -> list[LinkedAction]:
    """Actions linked to ``objective`` (its page shows them); MEETING_READ needed."""
    thesis = _thesis(objective)
    _require(user, Action.MEETING_READ, thesis, "objective_actions")
    actions = list(
        ActionItem.objects.filter(
            objective=objective, thesis=thesis, meeting__in=_readable_meetings(user, thesis)
        )
        .select_related("meeting", "assignee")
        .order_by("due_date", "pk")
    )
    opened = vault.open_texts(
        user, thesis, [(a, "title") for a in actions], action=Action.MEETING_READ
    )
    today = timezone.localdate()
    return [
        LinkedAction(a, a.meeting, opened.get((a.pk, "title"), ""), _overdue(a, today))
        for a in actions
    ]


def dashboard_block(request: Any, thesis: Thesis) -> dict[str, Any]:
    """The « Meetings » card and its tiles: next meeting (one I attend first, its
    date and kind only), overdue actions. Opens the titles of MY overdue actions
    only: the dashboard opens sealed texts once (frontend step 8a)."""
    user = request.user
    _require(user, Action.MEETING_READ, thesis, "dashboard")
    readable = _readable_meetings(user, thesis)
    upcoming = readable.filter(
        status=Meeting.Status.SCHEDULED, starts_at__gte=timezone.now()
    ).order_by("starts_at", "pk")
    following = upcoming.filter(attendees__user_id=user.pk).first() or upcoming.first()
    overdue = list(overdue_actions(thesis).filter(meeting__in=readable).select_related("meeting"))
    mine = [a for a in overdue if a.assignee_id == user.pk]
    items: list[tuple[Any, str]] = [(a, "title") for a in mine]
    opened = vault.open_texts(user, thesis, items, action=Action.MEETING_READ)
    return {
        "next_meeting": following,
        "next_is_mine": following is not None and is_attendee(user, following),
        "overdue_count": len(overdue),
        "my_overdue": [(a, _shown(opened.get((a.pk, "title"), ""))) for a in mine],
    }


def _shown(value: Any) -> dict[str, Any]:
    """What the templates' meetings/_text.html expects (like views.Shown). Only
    readable meetings are shown (SR-01): a restricted text here is a bug, refused."""
    if value is vault.RESTRICTED:
        raise PermissionDenied("a text of an unreadable meeting reached a page")
    return {"text": str(value or "")}


# =========================================================================== create


def _create(
    *,
    thesis: Thesis,
    user: User,
    frame: dict[str, Any],
    level: ConfidentialityLevel,
    texts: Mapping[str, str],
    people: list[User],
    audit_action: AuditAction,
    **audit_details: Any,
) -> Meeting:
    with _sealing(user, thesis, level) as sealer:
        if Thesis.objects.select_for_update().get(pk=thesis.pk).status not in OPEN_THESIS:
            raise _frozen()
        meeting = Meeting(thesis=thesis, level=level.value, created_by=user, **frame)
        meeting.save(force_insert=True)
        for name, value in texts.items():
            if value:
                sealer.seal(meeting, name, value)
        for person in people:
            Attendee.objects.create(meeting=meeting, user=person, created_by=user)
        notify(meeting, Notify.INVITED, uuid.uuid4(), people, exclude=user)
        activity.record(
            kind="MEETING_PLANNED",
            obj=meeting,
            object_type="MEETING",
            thesis=thesis,
            actor=user,
        )
        audit(
            audit_action,
            actor=user,
            obj=meeting,
            kind=meeting.kind,
            level=meeting.level,
            location=meeting.location,
            attendees=len(people),
            **audit_details,
        )
    return meeting


def create_meeting(
    *,
    thesis: Thesis,
    user: User,
    kind: str,
    starts_at: dt.datetime,
    duration_minutes: int,
    location: str,
    level: ConfidentialityLevel,
    title: str,
    attendees: Iterable[User],
    presentation_venue: str = "",
    location_detail: str = "",
    visio_link: str = "",
) -> Meeting:
    _require(user, Action.MEETING_WRITE, thesis, "create")
    _check_open(thesis)
    frame = _clean_frame(
        thesis,
        {
            "kind": kind,
            "presentation_venue": presentation_venue,
            "starts_at": starts_at,
            "duration_minutes": duration_minutes,
            "location": location,
        },
    )
    level = _clean_level(level)
    texts = {
        "title": _clean_text(title, "title"),
        "location_detail": _clean_text(location_detail, "location_detail"),
        "visio_link": _clean_link(visio_link),
    }
    people = _check_attendees(thesis, attendees, level)
    return _create(
        thesis=thesis,
        user=user,
        frame=frame,
        level=level,
        texts=texts,
        people=people,
        audit_action=AuditAction.MEETING_CREATED,
    )


def duplicate_meeting(*, meeting: Meeting, user: User, starts_at: dt.datetime) -> Meeting:
    """A new meeting at ``starts_at`` with the same type, venue, place, duration,
    level, attendees and title; never the agenda, minutes, decisions, actions,
    documents, place detail or video link."""
    thesis = _require_writer(user, meeting, "duplicate")
    _check_open(thesis)
    source = Meeting.objects.get(pk=meeting.pk)
    frame = _clean_frame(
        thesis,
        {name: getattr(source, name) for name in FRAME_FIELDS} | {"starts_at": starts_at},
    )
    level = _level(source)
    title = vault.open_texts(user, thesis, [(source, "title")], action=Action.MEETING_READ).get(
        (source.pk, "title"), ""
    )
    if title is vault.RESTRICTED:
        raise _deny(user, "duplicate", "unreadable_level", obj=source)
    people = _check_attendees(thesis, _current_attendees(source), level)
    return _create(
        thesis=thesis,
        user=user,
        frame=frame,
        level=level,
        texts={"title": str(title)},
        people=people,
        audit_action=AuditAction.MEETING_DUPLICATED,
        source_id=str(source.pk),
    )


# =========================================================================== edit


def _current_texts(
    user: User, thesis: Thesis, meeting: Meeting, names: Iterable[str]
) -> dict[str, Any]:
    names = list(names)
    opened = vault.open_texts(
        user, thesis, [(meeting, n) for n in names], action=Action.MEETING_READ
    )
    texts = {n: opened.get((meeting.pk, n), "") for n in names}
    if any(value is vault.RESTRICTED for value in texts.values()):
        raise _deny(user, "update", "unreadable_level", obj=meeting)
    return texts


def _has_minutes_or_items(meeting: Meeting) -> bool:
    return (
        meeting.minutes_version > 0
        or Decision.objects.filter(meeting=meeting).exists()
        or ActionItem.objects.filter(meeting=meeting).exists()
    )


def update_meeting(*, meeting: Meeting, user: User, changes: Mapping[str, Any]) -> list[str]:
    """Change the frame and texts of a meeting; returns the changed field names.

    A change of date, duration or place notifies the attendees (CHANGED); a
    change of anything in the .ics moves its SEQUENCE.
    """
    unknown = sorted(set(changes) - set(FRAME_FIELDS) - set(EDITABLE_TEXTS))
    if unknown:
        raise ValueError(f"{unknown} cannot be changed here")
    thesis = _require_writer(user, meeting, "update")
    _check_open(thesis)
    fresh = Meeting.objects.get(pk=meeting.pk)
    _not_cancelled(fresh)
    current_frame = {name: getattr(fresh, name) for name in FRAME_FIELDS}
    frame = _clean_frame(
        thesis, current_frame | {k: v for k, v in changes.items() if k in FRAME_FIELDS}
    )
    texts = {
        name: _clean_named_text(name, value)
        for name, value in changes.items()
        if name in EDITABLE_TEXTS
    }
    becomes_csi = frame["kind"] == Meeting.Kind.CSI and fresh.kind != Meeting.Kind.CSI
    if becomes_csi and _has_minutes_or_items(fresh):
        raise ValidationError(
            _("A meeting with minutes, decisions or actions cannot become a CSI meeting."),
            code="csi_pending",
        )
    current_texts = _current_texts(user, thesis, fresh, texts)
    changed = [f for f in FRAME_FIELDS if frame[f] != current_frame[f]]
    changed += [n for n in EDITABLE_TEXTS if n in texts and texts[n] != current_texts[n]]
    if not changed:
        return []
    with _sealing(user, thesis, _level(fresh)) as sealer:
        locked = _locked(fresh)
        if locked.updated_at != fresh.updated_at:
            raise _stale()
        for name in changed:
            if name in FRAME_FIELDS:
                setattr(locked, name, frame[name])
            elif texts[name]:
                sealer.seal(locked, name, texts[name])
            else:
                sealer.erase(locked, name)
        if CALENDAR_FIELDS & set(changed):
            locked.ics_sequence += 1
        locked.save()
        if NOTIFIED_FIELDS & set(changed):
            notify(locked, Notify.CHANGED, uuid.uuid4(), _current_attendees(locked), exclude=user)
            activity.record(
                kind="MEETING_CHANGED",
                obj=locked,
                object_type="MEETING",
                thesis=locked.thesis,
                actor=user,
            )
        audit(AuditAction.MEETING_UPDATED, actor=user, obj=locked, fields=changed)
    return changed


def change_level(
    *, meeting: Meeting, user: User, level: ConfidentialityLevel, recent_mfa: bool
) -> Meeting:
    """Move the meeting and every text of it (decisions, actions) to ``level``.

    A writer, with a second factor less than 10 minutes old; refused when an
    attendee could not read the new level.
    """
    thesis = _require_writer(user, meeting, "level")
    if not recent_mfa:
        raise ReauthenticationRequiredError()
    level = _clean_level(level)
    _check_open(thesis)
    fresh = Meeting.objects.get(pk=meeting.pk)
    _not_cancelled(fresh)
    previous = _level(fresh)
    if level == previous:
        return fresh
    _check_attendees(thesis, _current_attendees(fresh), level)
    with _sealing(user, thesis, level) as sealer:
        locked = _locked(fresh)
        if locked.level != previous.value:
            raise _stale()
        for name in MEETING_TEXTS:
            sealer.reseal(locked, name)
        for decision in Decision.objects.filter(meeting=locked):
            sealer.reseal(decision, "text")
        for action in ActionItem.objects.filter(meeting=locked):
            sealer.reseal(action, "title")
        locked.level = level.value
        locked.save()
        audit(
            AuditAction.MEETING_LEVEL_CHANGED,
            actor=user,
            obj=locked,
            previous=previous.value,
            level=level.value,
        )
    return locked


def set_attendees(*, meeting: Meeting, user: User, attendees: Iterable[User]) -> None:
    """Replace the attendees; the new ones are invited (INVITED)."""
    thesis = _require_writer(user, meeting, "attendees")
    _check_open(thesis)
    fresh = Meeting.objects.get(pk=meeting.pk)
    _not_cancelled(fresh)
    people = _check_attendees(thesis, attendees, _level(fresh))
    with transaction.atomic():
        locked = _locked(fresh)
        if locked.level != fresh.level:
            raise _stale()
        # Former members' rows stay (history): only current members are managed here.
        before = {u.pk for u in _current_attendees(locked)}
        after = {p.pk for p in people}
        Attendee.objects.filter(meeting=locked, user_id__in=before - after).delete()
        added = [p for p in people if p.pk not in before]
        for person in added:
            Attendee.objects.create(meeting=locked, user=person, created_by=user)
        if not added and before == after:
            return
        locked.save(update_fields=["updated_at"])
        notify(locked, Notify.INVITED, uuid.uuid4(), added, exclude=user)
        audit(
            AuditAction.MEETING_ATTENDEES_CHANGED,
            actor=user,
            obj=locked,
            added=len(added),
            removed=len(before - after),
        )


def cancel_meeting(*, meeting: Meeting, user: User) -> Meeting:
    """Cancel (never delete); the attendees are told, the .ics SEQUENCE moves."""
    thesis = _require_writer(user, meeting, "cancel")
    _check_open(thesis)
    with transaction.atomic():
        locked = _locked(meeting)
        locked.status = Meeting.Status.CANCELLED
        locked.cancelled_at = timezone.now()
        locked.ics_sequence += 1
        locked.save()
        notify(locked, Notify.CANCELLED, uuid.uuid4(), _current_attendees(locked), exclude=user)
        activity.record(
            kind="MEETING_CANCELLED",
            obj=locked,
            object_type="MEETING",
            thesis=locked.thesis,
            actor=user,
        )
        audit(AuditAction.MEETING_CANCELLED, actor=user, obj=locked)
    return locked


# =========================================================================== agenda, minutes


def save_agenda(*, meeting: Meeting, user: User, agenda: str) -> None:
    thesis = _require_writer(user, meeting, "agenda")
    _check_open(thesis)
    text = _clean_text(agenda, "agenda")
    fresh = Meeting.objects.get(pk=meeting.pk)
    _not_cancelled(fresh)
    with _sealing(user, thesis, _level(fresh)) as sealer:
        locked = _locked(fresh)
        if locked.level != fresh.level:
            raise _stale()
        if text:
            sealer.seal(locked, "agenda", text)
        else:
            sealer.erase(locked, "agenda")
        locked.save(update_fields=["updated_at"])
        audit(AuditAction.MEETING_AGENDA_UPDATED, actor=user, obj=locked, prefilled=0)


def _covers(meeting_level: ConfidentialityLevel, item_level: str) -> bool:
    """Every role reading the meeting reads the item: copying never widens a text."""
    try:
        level = ConfidentialityLevel(item_level)
    except ValueError:
        return False
    return LEVEL_ROLES[meeting_level] <= LEVEL_ROLES[level]


def _name(user: User) -> str:
    return user.get_full_name() or _("member")


def prefill_agenda(*, meeting: Meeting, user: User) -> int:
    """Append to the agenda the open actions and the overdue objectives of the
    thesis, each only if every role reading the meeting may read it and the user
    reads it; the copy is made here and sealed at the meeting's level. Returns
    the number of items copied."""
    thesis = _require_writer(user, meeting, "prefill")
    _check_open(thesis)
    fresh = Meeting.objects.get(pk=meeting.pk)
    _not_cancelled(fresh)
    level = _level(fresh)
    today = timezone.localdate()
    actions = [
        a
        for a in ActionItem.objects.filter(thesis=thesis, status=ActionItem.Status.OPEN)
        .exclude(meeting=fresh)
        .select_related("meeting", "assignee")
        .order_by("due_date", "pk")
        if _covers(level, a.meeting.level)
    ]
    objectives = [
        o
        for o in Objective.objects.filter(
            thesis=thesis,
            approved_at__isnull=False,
            status__in=[ObjectiveStatus.PLANNED, ObjectiveStatus.IN_PROGRESS],
            due_date__lt=today,
        ).order_by("due_date", "pk")
        if _covers(level, o.level)
    ]
    opened = vault.open_texts(
        user,
        thesis,
        [(fresh, "agenda"), *((a, "title") for a in actions)],
        action=Action.MEETING_READ,
    )
    titles = vault.open_texts(
        user, thesis, [(o, "title") for o in objectives], action=Action.OBJECTIVE_READ
    )
    current = opened.get((fresh.pk, "agenda"), "")
    if current is vault.RESTRICTED:
        raise _deny(user, "prefill", "unreadable_level", obj=fresh)
    lines: list[str] = []
    action_lines = [
        f"- {opened[(a.pk, 'title')]} ({_name(a.assignee)}, {a.due_date:%d/%m/%Y})"
        for a in actions
        if isinstance(opened.get((a.pk, "title")), str) and opened[(a.pk, "title")]
    ]
    objective_lines = [
        f"- {titles[(o.pk, 'title')]} ({o.due_date:%d/%m/%Y})"
        for o in objectives
        if isinstance(titles.get((o.pk, "title")), str) and titles[(o.pk, "title")]
    ]
    if action_lines:
        lines += [_("Open actions:"), *action_lines]
    if objective_lines:
        lines += ([""] if lines else []) + [_("Overdue objectives:"), *objective_lines]
    if not lines:
        raise ValidationError(
            _("No open action or overdue objective to copy at this level."),
            code="nothing_to_prefill",
        )
    block = "\n".join(lines)
    agenda = _clean_text(f"{current}\n\n{block}" if current else block, "agenda")
    count = len(action_lines) + len(objective_lines)
    with _sealing(user, thesis, level) as sealer:
        locked = _locked(fresh)
        if locked.updated_at != fresh.updated_at:
            raise _stale()
        sealer.seal(locked, "agenda", agenda)
        locked.save(update_fields=["updated_at"])
        audit(AuditAction.MEETING_AGENDA_UPDATED, actor=user, obj=locked, prefilled=count)
    return count


def save_minutes(*, meeting: Meeting, user: User, minutes: str, version: int) -> int:
    """Save the minutes over ``version`` (the one the editor started from);
    returns the new version. The first minutes notify every attendee; a later
    version those who had marked an earlier one (their mark is now outdated)."""
    thesis = _require_writer(user, meeting, "minutes")
    _check_open(thesis)
    fresh = Meeting.objects.get(pk=meeting.pk)
    _not_csi(fresh)
    _not_cancelled(fresh)
    _started(fresh)
    text = _clean_text(minutes, "minutes")
    with _sealing(user, thesis, _level(fresh)) as sealer:
        locked = _locked(fresh)
        if locked.minutes_version != version or locked.level != fresh.level:
            raise _stale()
        _not_csi(locked)
        _started(locked)
        sealer.seal(locked, "minutes", text)
        locked.minutes_version += 1
        locked.minutes_updated_at = timezone.now()
        locked.minutes_updated_by = user
        locked.save()
        if locked.minutes_version == 1:
            recipients = _current_attendees(locked)
        else:
            marked = MinutesAcknowledgement.objects.filter(meeting=locked).values_list(
                "user_id", flat=True
            )
            recipients = list(User.objects.filter(pk__in=list(marked)))
        notify(locked, Notify.MINUTES, uuid.uuid4(), recipients, exclude=user)
        if locked.minutes_version == 1:  # the first minutes: published
            activity.record(
                kind="MINUTES_PUBLISHED",
                obj=locked,
                object_type="MEETING",
                thesis=locked.thesis,
                actor=user,
            )
        audit(
            AuditAction.MEETING_MINUTES_UPDATED,
            actor=user,
            obj=locked,
            version=locked.minutes_version,
        )
    return locked.minutes_version


def acknowledge(
    *, meeting: Meeting, user: User, kind: str, version: int, recent_mfa: bool = False
) -> MinutesAcknowledgement:
    """An attendee marks ``version`` of the minutes read or approved. Approving
    is a validation: second factor less than 10 minutes old."""
    thesis = _thesis(meeting)
    _require(user, Action.MEETING_ACKNOWLEDGE, thesis, "acknowledge")
    if not is_attendee(user, meeting):
        raise _deny(user, "acknowledge", "not_attendee", obj=meeting)
    if kind not in Ack.values:
        raise ValidationError(_("Unknown mark."), code="kind")
    if kind == Ack.APPROVED and not recent_mfa:
        raise ReauthenticationRequiredError()
    with transaction.atomic():
        locked = _locked(meeting)
        _not_csi(locked)
        _started(locked)
        if locked.minutes_version == 0:
            raise ValidationError(_("There are no minutes yet."), code="no_minutes")
        if version != locked.minutes_version:
            raise _stale()
        existing = set(
            MinutesAcknowledgement.objects.filter(
                meeting=locked, user=user, minutes_version=version
            ).values_list("kind", flat=True)
        )
        if kind in existing or Ack.APPROVED in existing:
            raise ValidationError(
                _("You already marked this version."), code="already_acknowledged"
            )
        mark = MinutesAcknowledgement.objects.create(
            meeting=locked, user=user, minutes_version=version, kind=kind, created_by=user
        )
        audit(
            AuditAction.MEETING_MINUTES_ACKNOWLEDGED,
            actor=user,
            obj=locked,
            kind=kind,
            version=version,
        )
    return mark


# =========================================================================== decisions, actions


def add_decision(*, meeting: Meeting, user: User, text: str) -> Decision:
    thesis = _require_writer(user, meeting, "decision")
    _check_open(thesis)
    fresh = Meeting.objects.get(pk=meeting.pk)
    _not_csi(fresh)
    _not_cancelled(fresh)
    _started(fresh)
    body = _clean_text(text, "decision")
    with _sealing(user, thesis, _level(fresh)) as sealer:
        locked = _locked(fresh)
        if locked.level != fresh.level:
            raise _stale()
        decision = Decision(meeting=locked, thesis=thesis, created_by=user)
        decision.save(force_insert=True)
        sealer.seal(decision, "text", body)
        audit(
            AuditAction.MEETING_DECISION_ADDED, actor=user, obj=locked, decision_id=str(decision.pk)
        )
    return decision


def withdraw_decision(*, decision: Decision, user: User) -> Decision:
    meeting = Meeting.objects.get(pk=decision.meeting_id)
    thesis = _require_writer(user, meeting, "withdraw_decision")
    _check_open(thesis)
    with transaction.atomic():
        locked_meeting = _locked(meeting)
        locked = Decision.objects.select_for_update().get(pk=decision.pk)
        if locked.withdrawn_at is not None:
            raise ValidationError(_("This decision was already withdrawn."), code="withdrawn")
        locked.withdrawn_at = timezone.now()
        locked.save(update_fields=["withdrawn_at", "updated_at"])
        audit(
            AuditAction.MEETING_DECISION_WITHDRAWN,
            actor=user,
            obj=locked_meeting,
            decision_id=str(locked.pk),
        )
    return locked


def add_action(
    *,
    meeting: Meeting,
    user: User,
    title: str,
    assignee: User,
    due_date: dt.date,
    objective: Objective | None = None,
) -> ActionItem:
    thesis = _require_writer(user, meeting, "action")
    _check_open(thesis)
    fresh = Meeting.objects.get(pk=meeting.pk)
    _not_csi(fresh)
    _not_cancelled(fresh)
    _started(fresh)
    text = _clean_text(title, "action")
    person = _check_assignee(thesis, assignee)
    due = _check_due(thesis, due_date)
    linked = _check_objective(thesis, objective)
    with _sealing(user, thesis, _level(fresh)) as sealer:
        locked = _locked(fresh)
        if locked.level != fresh.level:
            raise _stale()
        action = ActionItem(
            meeting=locked,
            thesis=thesis,
            assignee=person,
            due_date=due,
            objective=linked,
            created_by=user,
        )
        action.save(force_insert=True)
        sealer.seal(action, "title", text)
        activity.record(
            kind="ACTION_ASSIGNED",
            obj=action,
            object_type="ACTION",
            thesis=thesis,
            actor=user,
        )
        audit(
            AuditAction.MEETING_ACTION_ADDED,
            actor=user,
            obj=locked,
            action_id=str(action.pk),
            objective_linked=linked is not None,
        )
    return action


def set_action_status(*, action: ActionItem, user: User, status: str) -> ActionItem:
    """Its assignee or a writer of the meeting marks an action open, done or dropped."""
    meeting = Meeting.objects.get(pk=action.meeting_id)
    thesis = _thesis(meeting)
    _require(user, Action.MEETING_READ, thesis, "action_status")
    if action.assignee_id != user.pk and not is_writer(user, meeting):
        raise _deny(user, "action_status", "not_assignee", obj=meeting)
    if status not in ActionItem.Status.values:
        raise ValidationError(_("Unknown status."), code="status")
    with transaction.atomic():
        locked_meeting = _locked(meeting, allow_cancelled=True)
        locked = ActionItem.objects.select_for_update().get(pk=action.pk)
        if locked.status == status:
            return locked
        previous = locked.status
        locked.status = status
        locked.done_at = timezone.now() if status == ActionItem.Status.DONE else None
        locked.save(update_fields=["status", "done_at", "updated_at"])
        audit(
            AuditAction.MEETING_ACTION_UPDATED,
            actor=user,
            obj=locked_meeting,
            action_id=str(locked.pk),
            fields=["status"],
            previous=previous,
            status=status,
        )
    return locked


# =========================================================================== documents


def link_document(*, meeting: Meeting, user: User, document: Document) -> MeetingDocument:
    """Link a vault document the user may read; linking changes nobody's access."""
    thesis = _require_writer(user, meeting, "link_document")
    _check_open(thesis)
    if document.thesis_id != thesis.pk:
        raise _deny(user, "link_document", "other_thesis", obj=meeting)
    if not can_access_document(user, document):
        raise _deny(user, "link_document", "unreadable_document", obj=meeting)
    with transaction.atomic():
        locked = _locked(meeting)
        if MeetingDocument.objects.filter(meeting=locked, document=document).exists():
            raise ValidationError(_("This document is already linked."), code="already_linked")
        link = MeetingDocument.objects.create(meeting=locked, document=document, created_by=user)
        activity.record(
            kind="DOCUMENT_LINKED",
            obj=link,
            object_type="MEETING_DOCUMENT",
            thesis=thesis,
            actor=user,
        )
        audit(
            AuditAction.MEETING_DOCUMENT_LINKED,
            actor=user,
            obj=locked,
            document_id=str(document.pk),
        )
    return link


def unlink_document(*, link: MeetingDocument, user: User) -> None:
    meeting = Meeting.objects.get(pk=link.meeting_id)
    thesis = _require_writer(user, meeting, "unlink_document")
    _check_open(thesis)
    with transaction.atomic():
        locked = _locked(meeting)
        row = MeetingDocument.objects.select_for_update().filter(pk=link.pk).first()
        if row is None:
            raise ValidationError(_("This document is no longer linked."), code="stale")
        document_id = str(row.document_id)
        row.delete()
        audit(
            AuditAction.MEETING_DOCUMENT_UNLINKED, actor=user, obj=locked, document_id=document_id
        )


# =========================================================================== .ics


def record_ics_export(*, meeting: Meeting, user: User) -> None:
    """An export (CLAUDE.md rule 8): audited, MEETING_READ needed."""
    thesis = _thesis(meeting)
    _require(user, Action.MEETING_READ, thesis, "ics")
    with transaction.atomic():
        audit(AuditAction.MEETING_ICS_EXPORTED, actor=user, obj=meeting)
