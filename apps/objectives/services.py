"""Business logic of objectives (M4).

Rights are decided HERE (accounts.permissions), even when a view checked too:
- create, edit, drop, progress: OBJECTIVE_WRITE (the student);
- approve, acknowledge changes: VALIDATE_OBJECTIVES (the director) AND a second
  factor less than 10 minutes old (``recent_mfa``, given by the view);
- comment, read comments: OBJECTIVE_COMMENT (the CSI member has not);
- read objectives, revisions, progress: OBJECTIVE_READ AND the objective's level
  (SR-01: « on ne voit l'existence que de ce qu'on peut lire »): an objective of
  another level is neither listed, counted nor reachable, and its page answers
  as an unknown id (Objective.objects.readable_by). The statistics are those of
  the objectives the reader reads (``partial`` says so, never a total).
A refusal is audited (OBJECTIVE_ACCESS_DENIED) in its own transaction, then
PermissionDenied is raised; the vault refuses on its own too (level).

Every text goes through the vault (vault.services.sealing / open_texts), at the
objective's level; the audit log gets field names and counts, never a text.

Lock order (CLAUDE.md): KEK lock (opened by sealing()), thesis row, objective
row, then audit() LAST. Current texts are read BEFORE the sealing block
(open_texts audits a CONFIDENTIAL read, which must not precede a seal in the
same transaction); a concurrent change in between is detected (``stale``).
"""

from __future__ import annotations

import datetime as dt
import unicodedata
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any, NamedTuple

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import ProtectedError, Q
from django.utils import timezone
from django.utils.translation import gettext as _

from apps.accounts.models import Thesis, User
from apps.accounts.permissions import Action, can, can_access_document, readable_levels, sees
from apps.accounts.statuses import open_statuses
from apps.activity import events as activity
from apps.core.confidentiality import ConfidentialityLevel
from apps.vault import services as vault
from apps.vault.audit import AuditAction, audit
from apps.vault.errors import ReauthenticationRequiredError
from apps.vault.models import Document

from . import stats as stats_module
from . import years
from .models import (
    OBJECTIVE_LEVELS,
    RESULT_STATUSES,
    TEXT_FIELDS,
    Category,
    Objective,
    ObjectiveComment,
    ObjectiveRevision,
    ProgressUpdate,
    RevisedField,
    Status,
    ThesisYear,
)


class SyncResult(NamedTuple):
    created: int
    updated: int
    deleted: int
    kept: int  # years past the planned end that still hold objectives


def _expected(thesis: Thesis) -> dict[int, dict[str, dt.date]]:
    count = years.year_count(thesis.cifre_start_date, thesis.planned_end_date)
    expected = {}
    for number in range(1, count + 1):
        cifre_start, cifre_end = years.cifre_bounds(thesis.cifre_start_date, number)
        academic_start, academic_end = years.academic_bounds(thesis.first_enrollment_date, number)
        expected[number] = {
            "cifre_start": cifre_start,
            "cifre_end": cifre_end,
            "academic_start": academic_start,
            "academic_end": academic_end,
        }
    return expected


def sync_years(thesis: Thesis) -> SyncResult:
    """Make the ThesisYear rows match the thesis dates; idempotent.

    Existing rows keep their id (objectives point to them) and get new dates;
    missing years are created; a year past the planned end is deleted unless
    objectives still point to it (PROTECT): it is then kept and counted.
    The thesis row is locked: two concurrent syncs of one thesis run one
    after the other. No vault call: safe after an audit() in the caller's
    transaction (create_thesis).
    """
    with transaction.atomic():
        locked = Thesis.objects.select_for_update().get(pk=thesis.pk)
        expected = _expected(locked)
        created = updated = deleted = kept = 0
        existing = {y.number: y for y in ThesisYear.objects.filter(thesis=locked)}
        for number, fields in expected.items():
            year = existing.get(number)
            if year is None:
                ThesisYear.objects.create(thesis=locked, number=number, **fields)
                created += 1
            elif any(getattr(year, name) != value for name, value in fields.items()):
                for name, value in fields.items():
                    setattr(year, name, value)
                year.save(update_fields=[*fields, "updated_at"])
                updated += 1
        for number, year in sorted(existing.items()):
            if number in expected:
                continue
            try:
                with transaction.atomic():
                    year.delete()
                deleted += 1
            except ProtectedError:
                kept += 1
    return SyncResult(created, updated, deleted, kept)


def _year_containing(thesis: Thesis, start: str, end: str) -> ThesisYear | None:
    today = timezone.localdate()  # the date in Paris (TIME_ZONE)
    return (
        ThesisYear.objects.filter(thesis=thesis, **{f"{start}__lte": today, f"{end}__gte": today})
        .order_by("number")
        .first()
    )


def current_year(thesis: Thesis) -> ThesisYear | None:
    """The CIFRE year containing today's date in Paris; None outside every year."""
    return _year_containing(thesis, "cifre_start", "cifre_end")


def current_academic_year(thesis: Thesis) -> ThesisYear | None:
    """The academic year containing today's date in Paris; None outside every year."""
    return _year_containing(thesis, "academic_start", "academic_end")


# =========================================================================== validation

OPEN_THESIS = open_statuses("objectives")  # apps/accounts/statuses.py
TEXT_LIMITS = {"title": (1, 200), "description": (0, 4000), "success_criterion": (0, 1000)}
NOTE_MAX = 2000
COMMENT_MAX = 2000
REASON_MIN, REASON_MAX = 10, 500
EDITABLE = tuple(f.value for f in RevisedField if f is not RevisedField.STATUS)
_ALLOWED_CONTROLS = frozenset("\n\t")


def _clean_text(value: Any, code: str, *, minimum: int, maximum: int) -> str:
    """NFC, CRLF -> LF, stripped; no control character except newline and tab."""
    if not isinstance(value, str):
        raise ValidationError(_("This text is invalid."), code=code)
    text = unicodedata.normalize("NFC", value.replace("\r\n", "\n")).strip()
    if any(unicodedata.category(c) == "Cc" and c not in _ALLOWED_CONTROLS for c in text):
        raise ValidationError(_("The text contains a control character."), code=code)
    if not minimum <= len(text) <= maximum:
        raise ValidationError(
            _("Between %(min)d and %(max)d characters.") % {"min": minimum, "max": maximum},
            code=code,
        )
    return text


def _clean_reason(reason: Any) -> str:
    if not isinstance(reason, str) or len(reason.strip()) < REASON_MIN:
        raise ValidationError(
            _("A reason of at least %(min)d characters is required.") % {"min": REASON_MIN},
            code="reason_required",
        )
    return _clean_text(reason, "reason", minimum=REASON_MIN, maximum=REASON_MAX)


def _clean_date(value: Any, code: str) -> dt.date:
    if isinstance(value, dt.datetime) or not isinstance(value, dt.date):
        raise ValidationError(_("A date is required."), code=code)
    return value


def _clean_field(name: str, value: Any) -> Any:
    if name in TEXT_LIMITS:
        minimum, maximum = TEXT_LIMITS[name]
        return _clean_text(value, name, minimum=minimum, maximum=maximum)
    if name == "category":
        if value not in Category.values:
            raise ValidationError(_("Unknown category."), code="category")
        return Category(value)
    if name == "due_date":
        return _clean_date(value, "due_date")
    if name == "weight":
        if type(value) is not int or not 1 <= value <= 3:
            raise ValidationError(_("The weight is 1, 2 or 3."), code="weight")
        return value
    if name == "level":
        if not isinstance(value, ConfidentialityLevel) or value not in OBJECTIVE_LEVELS:
            raise ValidationError(
                _("An objective is Public, Internal LIRMM or Confidential."), code="level"
            )
        return value
    raise ValueError(f"{name!r} cannot be changed")


def _as_clear(value: Any) -> str:
    if isinstance(value, dt.date):
        return value.isoformat()
    if isinstance(value, ConfidentialityLevel | Category | Status):
        return str(value.value)
    return str(value)


# =========================================================================== rights


def _deny(user: Any, operation: str, reason: str, **kwargs: Any) -> PermissionDenied:
    """OBJECTIVE_ACCESS_DENIED in its own transaction (kept although an exception follows)."""
    with transaction.atomic():
        audit(
            AuditAction.OBJECTIVE_ACCESS_DENIED,
            actor=user,
            operation=operation,
            reason=reason,
            **kwargs,
        )
    return PermissionDenied()


def _require(user: Any, action: Action, thesis: Thesis, operation: str) -> None:
    if not can(user, action, thesis):
        raise _deny(user, operation, "missing_right", thesis=thesis)


def _thesis(objective: Objective) -> Thesis:
    return Thesis.objects.get(pk=objective.thesis_id)


def _locked(objective: Objective) -> tuple[Thesis, Objective]:
    """Lock the thesis row, THEN the objective row; refuse a finished thesis."""
    thesis = Thesis.objects.select_for_update().get(pk=objective.thesis_id)
    if thesis.status not in OPEN_THESIS:
        raise ValidationError(_("The thesis is finished: its objectives are frozen."), "frozen")
    locked = Objective.objects.select_for_update().get(pk=objective.pk)
    return thesis, locked


def _check_open(thesis: Thesis) -> None:
    if Thesis.objects.get(pk=thesis.pk).status not in OPEN_THESIS:
        raise ValidationError(_("The thesis is finished: its objectives are frozen."), "frozen")


def _not_dropped(objective: Objective) -> None:
    if objective.status == Status.DROPPED:
        raise ValidationError(_("This objective was dropped."), code="dropped")


def _level(objective: Objective) -> ConfidentialityLevel:
    return ConfidentialityLevel(objective.level)


# =========================================================================== reading


def objective_for(user: User, thesis: Thesis, objective_id: Any, *, operation: str) -> Objective:
    """The objective ``objective_id`` of ``thesis`` if ``user`` may read objectives.

    Unknown, malformed, or of another thesis than the URL's: the same audited
    PermissionDenied (IDOR: an id never crosses theses).
    """
    try:
        objective = Objective.objects.filter(pk=objective_id, thesis=thesis).first()
    except (ValueError, ValidationError):
        objective = None
    if objective is None:
        raise _deny(user, operation, "unknown_objective", thesis=thesis)
    _require(user, Action.OBJECTIVE_READ, thesis, operation)
    _require_level(user, objective, operation)
    return objective


def _require_level(user: Any, objective: Objective, operation: str) -> None:
    """SR-01: an objective of a level ``user`` does not read does not exist for
    them: the same PermissionDenied as an unknown id (the journal says why)."""
    if not sees(user, objective, Action.OBJECTIVE_READ):
        raise _deny(user, operation, "unreadable_level", thesis=_thesis(objective))


def _partial(user: Any, thesis: Thesis) -> bool:
    """True when ``user`` does not read every level an objective may have: the
    statistics then say they count the objectives the user reads. A function of
    the roles only, never of the objectives (two worlds)."""
    return not set(OBJECTIVE_LEVELS) <= readable_levels(user, thesis)


def objective_texts(
    user: User, thesis: Thesis, objectives: Iterable[Objective]
) -> dict[uuid.UUID, dict[str, Any]]:
    """Title, description and criterion of each objective: str ("" if none) or RESTRICTED."""
    objectives = list(objectives)
    opened = vault.open_texts(
        user,
        thesis,
        [(o, f.value) for o in objectives for f in TEXT_FIELDS],
        action=Action.OBJECTIVE_READ,
    )
    return {
        o.pk: {f.value: opened.get((o.pk, f.value), "") for f in TEXT_FIELDS} for o in objectives
    }


@dataclass(frozen=True)
class RevisionView:
    revision: ObjectiveRevision
    reason: Any  # str or vault.RESTRICTED
    old_value: Any
    new_value: Any


def revisions_for(user: User, objective: Objective) -> list[RevisionView]:
    return objective_detail(user, objective).revisions


@dataclass(frozen=True)
class CommentView:
    comment: ObjectiveComment
    body: Any


def comments_for(user: User, objective: Objective) -> list[CommentView]:
    """The comment thread: OBJECTIVE_COMMENT_READ only (never the CSI member); M13a:
    a read right, so a closed thesis keeps it readable."""
    thesis = _thesis(objective)
    _require(user, Action.OBJECTIVE_COMMENT_READ, thesis, "comments")
    _require_level(user, objective, "comments")
    comments = list(ObjectiveComment.objects.filter(objective=objective).order_by("created_at"))
    opened = vault.open_texts(
        user, thesis, [(c, "body") for c in comments], action=Action.OBJECTIVE_COMMENT_READ
    )
    return [CommentView(comment=c, body=opened.get((c.pk, "body"), "")) for c in comments]


@dataclass(frozen=True)
class ProgressView:
    update: ProgressUpdate
    note: Any
    evidence: Document | None  # None when absent OR not readable (never hinted, SR-01)


def progress_for(user: User, objective: Objective) -> list[ProgressView]:
    return objective_detail(user, objective).progress


@dataclass(frozen=True)
class ObjectiveDetail:
    objective: Objective
    texts: dict[str, Any]  # title, description, success_criterion: str or RESTRICTED
    revisions: list[RevisionView]
    progress: list[ProgressView]


_REVISION_TEXTS = ("reason", "old_value", "new_value")


def objective_detail(user: User, objective: Objective) -> ObjectiveDetail:
    """Everything the detail page shows, its texts opened in ONE vault call (one
    SEALED_TEXT_READ per request for a CONFIDENTIAL objective). Comments are not
    here: they need OBJECTIVE_COMMENT (comments_for, a fragment of their own)."""
    thesis = _thesis(objective)
    _require(user, Action.OBJECTIVE_READ, thesis, "detail")
    _require_level(user, objective, "detail")
    revisions = list(ObjectiveRevision.objects.filter(objective=objective).order_by("created_at"))
    updates = list(
        ProgressUpdate.objects.filter(objective=objective)
        .select_related("evidence")
        .order_by("date", "created_at")
    )
    items: list[tuple[Any, str]] = [(objective, f.value) for f in TEXT_FIELDS]
    items += [(r, name) for r in revisions for name in _REVISION_TEXTS]
    items += [(u, "note") for u in updates]
    opened = vault.open_texts(user, thesis, items, action=Action.OBJECTIVE_READ)
    text_fields = {f.value for f in TEXT_FIELDS}
    revision_views = []
    for r in revisions:
        sealed = r.field in text_fields
        revision_views.append(
            RevisionView(
                revision=r,
                reason=opened.get((r.pk, "reason"), ""),
                old_value=opened.get((r.pk, "old_value"), "") if sealed else r.old_clear,
                new_value=opened.get((r.pk, "new_value"), "") if sealed else r.new_clear,
            )
        )
    progress_views = []
    for u in updates:
        visible = u.evidence if u.evidence and can_access_document(user, u.evidence) else None
        progress_views.append(
            ProgressView(
                update=u,
                note=opened.get((u.pk, "note"), ""),
                evidence=visible,
            )
        )
    return ObjectiveDetail(
        objective=objective,
        texts={f.value: opened.get((objective.pk, f.value), "") for f in TEXT_FIELDS},
        revisions=revision_views,
        progress=progress_views,
    )


# =========================================================================== writing


def create_objective(
    *,
    thesis: Thesis,
    user: User,
    year: ThesisYear,
    title: str,
    description: str = "",
    success_criterion: str = "",
    category: str,
    due_date: dt.date,
    weight: int = 1,
    level: ConfidentialityLevel = ConfidentialityLevel.PUBLIC,
) -> Objective:
    _require(user, Action.OBJECTIVE_WRITE, thesis, "create")
    _check_open(thesis)
    values = {
        "title": title,
        "description": description,
        "success_criterion": success_criterion,
        "category": category,
        "due_date": due_date,
        "weight": weight,
        "level": level,
    }
    cleaned = {name: _clean_field(name, value) for name, value in values.items()}
    if year.thesis_id != thesis.pk:
        raise ValidationError(_("This year belongs to another thesis."), code="year")
    with vault.sealing(
        user=user, thesis=thesis, level=cleaned["level"], action=Action.OBJECTIVE_WRITE
    ) as sealer:
        if Thesis.objects.select_for_update().get(pk=thesis.pk).status not in OPEN_THESIS:
            raise ValidationError(_("The thesis is finished: its objectives are frozen."), "frozen")
        objective = Objective(
            thesis=thesis,
            year=year,
            category=cleaned["category"],
            due_date=cleaned["due_date"],
            weight=cleaned["weight"],
            level=cleaned["level"].value,
            created_by=user,
        )
        objective.save(force_insert=True)
        for field in TEXT_FIELDS:
            if cleaned[field.value]:
                sealer.seal(objective, field.value, cleaned[field.value])
        activity.record(
            kind="OBJECTIVE_CREATED",
            obj=objective,
            object_type="OBJECTIVE",
            thesis=thesis,
            actor=user,
        )
        audit(
            AuditAction.OBJECTIVE_CREATED,
            actor=user,
            obj=objective,
            year=year.number,
            level=objective.level,
            category=objective.category,
        )
    return objective


def _current_values(user: User, thesis: Thesis, objective: Objective) -> dict[str, Any]:
    opened = objective_texts(user, thesis, [objective])[objective.pk]
    if any(value is vault.RESTRICTED for value in opened.values()):
        raise _deny(user, "update", "unreadable_level", obj=objective)
    return {
        **opened,
        "category": Category(objective.category),
        "due_date": objective.due_date,
        "weight": objective.weight,
        "level": _level(objective),
    }


def _reseal_everything(sealer: vault.TextSealer, objective: Objective) -> None:
    """Move every text of the objective and of its dependents to the sealer's level."""
    for field in TEXT_FIELDS:
        sealer.reseal(objective, field.value)
    for revision in ObjectiveRevision.objects.filter(objective=objective):
        for name in ("reason", "old_value", "new_value"):
            sealer.reseal(revision, name)
    for update in ProgressUpdate.objects.filter(objective=objective):
        sealer.reseal(update, "note")
    for comment in ObjectiveComment.objects.filter(objective=objective):
        sealer.reseal(comment, "body")


def update_objective(
    *, objective: Objective, user: User, changes: Mapping[str, Any], reason: str = ""
) -> list[ObjectiveRevision]:
    """Change the definition of an objective; returns the revisions created.

    A draft is edited freely (OBJECTIVE_UPDATED). Once approved, any real change
    needs a reason: one ObjectiveRevision per changed field (reason and text
    values sealed), ``changed_since_approval`` set (OBJECTIVE_REVISED). A
    change of level re-seals every text of the objective and its dependents.
    """
    unknown = sorted(set(changes) - set(EDITABLE))
    if unknown:
        raise ValueError(f"{unknown} cannot be changed here")
    thesis = _thesis(objective)
    _require(user, Action.OBJECTIVE_WRITE, thesis, "update")
    _check_open(thesis)
    cleaned = {name: _clean_field(name, value) for name, value in changes.items()}
    fresh = Objective.objects.get(pk=objective.pk)
    _not_dropped(fresh)
    current = _current_values(user, thesis, fresh)
    changed = [f for f in EDITABLE if f in cleaned and cleaned[f] != current[f]]
    if not changed:
        return []
    reason_text = _clean_reason(reason) if fresh.is_approved else ""
    level = cleaned.get("level", current["level"])
    with vault.sealing(
        user=user, thesis=thesis, level=level, action=Action.OBJECTIVE_WRITE
    ) as sealer:
        _thesis_row, locked = _locked(fresh)
        if locked.updated_at != fresh.updated_at:
            raise ValidationError(_("The objective changed meanwhile: try again."), code="stale")
        if level != current["level"]:
            _reseal_everything(sealer, locked)
        for name in changed:
            if name in TEXT_LIMITS:
                if cleaned[name]:
                    sealer.seal(locked, name, cleaned[name])
                else:
                    sealer.erase(locked, name)
            elif name == "level":
                locked.level = level.value
            else:
                setattr(locked, name, cleaned[name])
        revisions: list[ObjectiveRevision] = []
        if locked.is_approved:
            for name in changed:
                revision = ObjectiveRevision(
                    thesis=thesis, objective=locked, changed_by=user, field=name, created_by=user
                )
                if name not in TEXT_LIMITS:
                    revision.old_clear = _as_clear(current[name])
                    revision.new_clear = _as_clear(cleaned[name])
                revision.save(force_insert=True)
                sealer.seal(revision, "reason", reason_text)
                if name in TEXT_LIMITS:
                    sealer.seal(revision, "old_value", current[name])
                    sealer.seal(revision, "new_value", cleaned[name])
                revisions.append(revision)
            locked.changed_since_approval = True
        locked.save()
        activity.record(
            kind="OBJECTIVE_CHANGED",
            obj=locked,
            object_type="OBJECTIVE",
            thesis=thesis,
            actor=user,
        )
        if locked.is_approved:
            audit(
                AuditAction.OBJECTIVE_REVISED,
                actor=user,
                obj=locked,
                fields=changed,
                revisions=len(revisions),
            )
        else:
            audit(AuditAction.OBJECTIVE_UPDATED, actor=user, obj=locked, fields=changed)
    return revisions


def _require_validator(user: User, thesis: Thesis, operation: str, recent_mfa: bool) -> None:
    _require(user, Action.VALIDATE_OBJECTIVES, thesis, operation)
    if not recent_mfa:
        raise ReauthenticationRequiredError()


def _approve_locked(objective: Objective, user: User, now: dt.datetime) -> None:
    _not_dropped(objective)
    if objective.is_approved:
        raise ValidationError(_("This objective is already approved."), code="already_approved")
    objective.approved_by, objective.approved_at = user, now
    objective.save(update_fields=["approved_by", "approved_at", "updated_at"])


def approve_objective(*, objective: Objective, user: User, recent_mfa: bool) -> Objective:
    """The director approves a draft; needs a second factor less than 10 minutes old."""
    thesis = _thesis(objective)
    _require_validator(user, thesis, "approve", recent_mfa)
    with transaction.atomic():
        _thesis_row, locked = _locked(objective)
        _approve_locked(locked, user, timezone.now())
        audit(AuditAction.OBJECTIVE_APPROVED, actor=user, obj=locked)
    return locked


def approve_year(*, year: ThesisYear, user: User, recent_mfa: bool) -> int:
    """Approve every pending (not dropped) objective of ``year``; returns how many."""
    thesis = Thesis.objects.get(pk=year.thesis_id)
    _require_validator(user, thesis, "approve", recent_mfa)
    with transaction.atomic():
        Thesis.objects.select_for_update().get(pk=thesis.pk)
        _check_open(thesis)
        pending = list(
            Objective.objects.select_for_update()
            .filter(year=year, approved_at__isnull=True)
            .exclude(status=Status.DROPPED)
            .order_by("pk")
        )
        now = timezone.now()
        for objective in pending:
            _approve_locked(objective, user, now)
        for objective in pending:  # audit() last: after every row is written
            audit(AuditAction.OBJECTIVE_APPROVED, actor=user, obj=objective)
    return len(pending)


def pending_acknowledgements(thesis: Thesis) -> list[Objective]:
    """Approved objectives changed or with a declared result, not yet acknowledged
    (listed in the director's weekly summary; no text)."""
    return list(
        Objective.objects.filter(thesis=thesis, approved_at__isnull=False)
        .filter(Q(changed_since_approval=True) | Q(result_declared=True))
        .order_by("due_date", "pk")
    )


def acknowledge_changes(*, objective: Objective, user: User, recent_mfa: bool) -> Objective:
    """The director takes note of the changes since approval and of a declared
    result; clears both mentions (the result itself stays as declared)."""
    thesis = _thesis(objective)
    _require_validator(user, thesis, "acknowledge", recent_mfa)
    with transaction.atomic():
        _thesis_row, locked = _locked(objective)
        if not (locked.changed_since_approval or locked.result_declared):
            raise ValidationError(_("Nothing to acknowledge."), code="nothing_to_acknowledge")
        result = locked.result_declared
        now = timezone.now()
        count = ObjectiveRevision.objects.filter(
            objective=locked, acknowledged_at__isnull=True
        ).update(acknowledged_at=now)
        locked.changed_since_approval = False
        locked.result_declared = False
        locked.acknowledged_by, locked.acknowledged_at = user, now
        locked.save(
            update_fields=[
                "changed_since_approval",
                "result_declared",
                "acknowledged_by",
                "acknowledged_at",
                "updated_at",
            ]
        )
        audit(
            AuditAction.OBJECTIVE_CHANGES_ACKNOWLEDGED,
            actor=user,
            obj=locked,
            revisions=count,
            result=result,
        )
    return locked


def drop_objective(*, objective: Objective, user: User, reason: str) -> Objective:
    """Drop (never delete) an objective; the reason is required and kept, sealed."""
    thesis = _thesis(objective)
    _require(user, Action.OBJECTIVE_WRITE, thesis, "drop")
    _check_open(thesis)
    _not_dropped(Objective.objects.get(pk=objective.pk))
    reason_text = _clean_reason(reason)
    with vault.sealing(
        user=user, thesis=thesis, level=_level(objective), action=Action.OBJECTIVE_WRITE
    ) as sealer:
        _thesis_row, locked = _locked(objective)
        _not_dropped(locked)
        if locked.level != objective.level:
            raise ValidationError(_("The objective changed meanwhile: try again."), code="stale")
        previous = locked.status
        revision = ObjectiveRevision(
            thesis=thesis,
            objective=locked,
            changed_by=user,
            field=RevisedField.STATUS,
            old_clear=previous,
            new_clear=Status.DROPPED,
            created_by=user,
        )
        revision.save(force_insert=True)
        sealer.seal(revision, "reason", reason_text)
        locked.status = Status.DROPPED
        if locked.is_approved:
            locked.changed_since_approval = True
        locked.save()
        audit(AuditAction.OBJECTIVE_DROPPED, actor=user, obj=locked, previous_status=previous)
    return locked


PROGRESS_STATUSES = tuple(s for s in Status if s is not Status.DROPPED)


def _is_result_change(before: str, after: str) -> bool:
    """A result declared, changed or withdrawn (one side is a result, and they differ)."""
    results = {s.value for s in RESULT_STATUSES}
    return before != after and (before in results or after in results)


def add_progress(
    *,
    objective: Objective,
    user: User,
    date: dt.date,
    progress: int,
    status: str,
    note: str = "",
    evidence: Document | None = None,
) -> ProgressUpdate:
    """A dated progress report (not a change of definition: no reason, no revision)."""
    thesis = _thesis(objective)
    _require(user, Action.OBJECTIVE_WRITE, thesis, "progress")
    _check_open(thesis)
    _not_dropped(Objective.objects.get(pk=objective.pk))
    day = _clean_date(date, "date")
    if day > timezone.localdate():  # the date in Paris
        raise ValidationError(_("A progress report cannot be dated in the future."), code="date")
    if type(progress) is not int or not 0 <= progress <= 100:
        raise ValidationError(_("Progress is between 0 and 100."), code="progress")
    if status not in [s.value for s in PROGRESS_STATUSES]:
        raise ValidationError(_("Choose a status."), code="status")
    note_text = _clean_text(note, "note", minimum=0, maximum=NOTE_MAX)
    if evidence is not None and (
        evidence.thesis_id != thesis.pk or not can_access_document(user, evidence)
    ):
        raise ValidationError(_("This document cannot be attached."), code="evidence")
    if status == Status.ACHIEVED:
        progress = 100
    with vault.sealing(
        user=user, thesis=thesis, level=_level(objective), action=Action.OBJECTIVE_WRITE
    ) as sealer:
        _thesis_row, locked = _locked(objective)
        _not_dropped(locked)
        if locked.level != objective.level:
            raise ValidationError(_("The objective changed meanwhile: try again."), code="stale")
        # A progress above 0 % is never « Planned » (also a CHECK in the database):
        # from PLANNED it starts the objective; back to PLANNED it is refused.
        if status == Status.PLANNED and progress > 0:
            if locked.status != Status.PLANNED:
                raise ValidationError(
                    _(
                        "An objective with progress cannot go back to “Planned”: keep "
                        "“In progress” or set the progress back to zero."
                    ),
                    code="planned_with_progress",
                )
            status = Status.IN_PROGRESS
        update = ProgressUpdate(
            thesis=thesis,
            objective=locked,
            date=day,
            progress=progress,
            status=status,
            evidence=evidence,
            created_by=user,
        )
        update.save(force_insert=True)
        if note_text:
            sealer.seal(update, "note", note_text)
        declared = locked.is_approved and _is_result_change(locked.status, status)
        locked.progress, locked.status = progress, status
        if declared:
            locked.result_declared = True
        locked.save(update_fields=["progress", "status", "result_declared", "updated_at"])
        activity.record(
            kind="PROGRESS_REPORTED",
            obj=locked,
            object_type="OBJECTIVE",
            thesis=thesis,
            actor=user,
        )
        audit(
            AuditAction.OBJECTIVE_PROGRESS_ADDED,
            actor=user,
            obj=locked,
            progress=progress,
            status=status,
            evidence=evidence is not None,
            result_declared=declared,
        )
    return update


def add_comment(*, objective: Objective, user: User, body: str) -> ObjectiveComment:
    """A comment of the supervision, sealed at the objective's level."""
    thesis = _thesis(objective)
    _require(user, Action.OBJECTIVE_COMMENT, thesis, "comment")
    _check_open(thesis)
    text = _clean_text(body, "body", minimum=1, maximum=COMMENT_MAX)
    with vault.sealing(
        user=user, thesis=thesis, level=_level(objective), action=Action.OBJECTIVE_COMMENT
    ) as sealer:
        _thesis_row, locked = _locked(objective)
        if locked.level != objective.level:
            raise ValidationError(_("The objective changed meanwhile: try again."), code="stale")
        comment = ObjectiveComment(thesis=thesis, objective=locked, author=user, created_by=user)
        comment.save(force_insert=True)
        sealer.seal(comment, "body", text)
        activity.record(
            kind="OBJECTIVE_COMMENTED",
            obj=comment,
            object_type="OBJECTIVE_COMMENT",
            thesis=thesis,
            actor=user,
        )
        audit(AuditAction.OBJECTIVE_COMMENT_ADDED, actor=user, obj=locked)
    return comment


# =========================================================================== statistics


@dataclass(frozen=True)
class YearStats:
    year: ThesisYear
    stats: stats_module.Stats
    monthly: list[stats_module.MonthPoint]


def stats_for(user: User, thesis: Thesis, year: ThesisYear) -> YearStats:
    """Statistics of one CIFRE year (no text is opened: none is needed to count)."""
    _require(user, Action.OBJECTIVE_READ, thesis, "stats")
    if year.thesis_id != thesis.pk:
        raise _deny(user, "stats", "other_thesis", thesis=thesis)
    objectives = list(
        Objective.objects.readable_by(user, thesis)
        .filter(year=year)
        .prefetch_related("progress_updates")
    )
    rows = [
        stats_module.Row(
            category=o.category,
            weight=o.weight,
            status=o.status,
            due_date=o.due_date,
            approved=o.is_approved,
            created_on=timezone.localdate(o.created_at),
            updates=tuple(
                stats_module.Update(u.date, u.progress, u.status)
                for u in sorted(o.progress_updates.all(), key=lambda u: (u.created_at, u.pk))
            ),
        )
        for o in objectives
    ]
    today = timezone.localdate()  # the date in Paris
    return YearStats(
        year=year,
        # A finished year is measured on its last day, like its chart's last point.
        stats=stats_module.compute_stats(rows, today, progress_day=min(today, year.cifre_end)),
        monthly=stats_module.monthly_progress(
            rows, first_day=year.cifre_start, last_day=year.cifre_end, today=today
        ),
    )


# =========================================================================== dashboard


@dataclass(frozen=True)
class Listed:
    objective: Objective
    texts: dict[str, Any]
    overdue: bool


@dataclass(frozen=True)
class Dashboard:
    year: ThesisYear
    years: list[ThesisYear]
    stats: YearStats
    objectives: list[Listed]  # of the year, those the user reads (SR-01)
    partial: bool  # the user does not read every objective level: say so
    overdue: list[Listed]  # of the year, approved, still open, due before today
    pending: list[Listed]  # every year: to acknowledge (the director's list only)


def _is_overdue(objective: Objective, today: dt.date) -> bool:
    return (
        objective.is_approved
        and objective.status in (Status.PLANNED, Status.IN_PROGRESS)
        and objective.due_date < today
    )


def dashboard(user: User, thesis: Thesis, year: ThesisYear) -> Dashboard:
    """The objectives page of one year; every title opened in ONE vault call."""
    year_stats = stats_for(user, thesis, year)  # rights and year checked there
    today = timezone.localdate()
    readable = Objective.objects.readable_by(user, thesis)
    of_year = list(readable.filter(year=year).order_by("due_date", "created_at"))
    readable_ids = set(readable.values_list("pk", flat=True))
    pending = (
        [o for o in pending_acknowledgements(thesis) if o.pk in readable_ids]
        if can(user, Action.VALIDATE_OBJECTIVES, thesis)
        else []
    )
    unique = {o.pk: o for o in [*of_year, *pending]}
    texts = objective_texts(user, thesis, unique.values())

    def listed(o: Objective) -> Listed:
        return Listed(objective=o, texts=texts[o.pk], overdue=_is_overdue(o, today))

    rows = [listed(o) for o in of_year]
    return Dashboard(
        year=year,
        years=list(ThesisYear.objects.filter(thesis=thesis).order_by("number")),
        stats=year_stats,
        objectives=rows,
        partial=_partial(user, thesis),
        overdue=[r for r in rows if r.overdue],
        pending=[listed(o) for o in pending],
    )


def dashboard_block(request: Any, thesis: Thesis) -> dict[str, Any]:
    """Data of the thesis dashboard block (accounts.views.DASHBOARD_BLOCKS): counts only."""
    year = (
        current_year(thesis) or ThesisYear.objects.filter(thesis=thesis).order_by("number").first()
    )
    if year is None:
        return {}
    result = stats_for(request.user, thesis, year)
    return {
        "year": year,
        "attainment": stats_module.as_percent(result.stats.attainment),
        "drafts": result.stats.drafts,
        "overdue": result.stats.overdue,
        "partial": _partial(request.user, thesis),
    }
