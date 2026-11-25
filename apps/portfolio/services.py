"""Business logic of the portfolio (M9a, M9b): trainings, the ADUM summary, the date
of the next re-registration (M9a); publication details, teaching, outreach, skills
and professional project, the ADUM summary extended and its BibTeX export (M9b)
(docs/modules/M09-portfolio.md).

M9b, rights (also decided here):
- publication details: read as their submission (SUBMISSION_READ and the
  submission's level: Submission.objects.readable_by, sees); written by the
  student (SUBMISSION_WRITE) while ``portfolio_publications`` is open (SETUP,
  ACTIVE, DEFENDED); sealed at the submission's level, resealed with it
  (apps/conferences/sealed.py);
- teaching and outreach: TEACHING_READ / OUTREACH_READ and the level (the whole
  supervision, never the CSI member); written by the student; notes: the student;
- skills and professional project: CAREER_NOTE_READ / CAREER_NOTE_WRITE, the
  student alone, sealed at PERSONAL_LEVEL; never in a total, the feed or ADUM;
- the BibTeX export: PORTFOLIO_EXPORT, second factor, journaled in numbers.

Rights are decided HERE (accounts.permissions), even when a view checked too:
- create, edit, change the level, set the ADUM state, set the re-registration
  date: TRAINING_WRITE (the student); the level change also needs a second factor
  less than 10 minutes old (``recent_mfa``, given by the view);
- read the trainings: TRAINING_READ AND the training's level (SR-01: « on ne voit
  l'existence que de ce qu'on peut lire »): a training of another level is
  neither listed nor counted, and its page answers as an unknown id
  (Training.objects.readable_by). The totals are those of the finished trainings
  the reader reads (``partial`` says so, a function of the roles only);
- the notes and the ADUM state: TRAINING_WRITE only (never the CSI member, nor a
  supervisor); a certificate: only to whoever reads that document;
- the ADUM summary and its CSV: PORTFOLIO_EXPORT (the student); the CSV also needs
  a second factor (rule 6, an export) and is journaled (PORTFOLIO_EXPORTED).
A refusal is audited (PORTFOLIO_ACCESS_DENIED) in its own transaction, then
PermissionDenied is raised; the vault refuses on its own too (level).

Every text goes through the vault (vault.services.sealing / open_texts), at the
training's level; the audit log gets field names, codes and counts, never a text.

Lock order (CLAUDE.md): KEK lock (opened by sealing()), thesis row, training row,
then audit() LAST. Current texts are read BEFORE the sealing block.
"""

from __future__ import annotations

import csv
import datetime as dt
import io
import re
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.utils import timezone
from django.utils.translation import gettext as _
from django.utils.translation import pgettext, pgettext_lazy

from apps.accounts.models import Thesis, User
from apps.accounts.permissions import Action, can, can_access_document, readable_levels, sees
from apps.accounts.statuses import is_open_for
from apps.conferences.models import Submission
from apps.core.confidentiality import ConfidentialityLevel
from apps.vault import services as vault
from apps.vault.audit import AuditAction, audit
from apps.vault.errors import ReauthenticationRequiredError
from apps.vault.models import Document

from .models import (
    DETAILS_TEXTS,
    HOURS_MAX,
    HOURS_MIN,
    OUTREACH_TEXTS,
    PERSONAL_LEVEL,
    PUBLICATION_CATEGORIES,
    SKILL_TEXTS,
    TEACHING_TEXTS,
    TEXT_FIELDS,
    AdumCategory,
    AdumStatus,
    CareerNote,
    Outreach,
    OutreachKind,
    PortfolioPolicy,
    PublicationDetails,
    SkillCategory,
    SkillNote,
    Teaching,
    TeachingKind,
    Training,
    TrainingCategory,
)

MODULE = "portfolio"  # apps/accounts/statuses.py
TEXT_LIMITS = {
    "title": (1, 200),
    "provider": (0, 200),
    "notes": (0, 2000),
    # M9b
    "course": (1, 200),
    "institution": (0, 200),
    "audience": (0, 200),
    "evidence": (0, 2000),
    "text": (0, 8000),
}
TRAINING_LIMITS = ("title", "provider", "notes")
PRIVATE_TEXTS = frozenset({"notes"})  # the student only (TRAINING_WRITE)
_ALLOWED_CONTROLS = frozenset("\n\t")
# A spreadsheet runs a cell that starts with one of these as a formula (CSV injection).
FORMULA_STARTS = ("=", "+", "-", "@", "\t", "\r")
BOM = "\ufeff"  # spreadsheets read the file as UTF-8 only with it


# =========================================================================== validation


def _clean_text(value: Any, name: str) -> str:
    """NFC, CRLF -> LF, stripped; no control character except newline and tab."""
    minimum, maximum = TEXT_LIMITS[name]
    if not isinstance(value, str):
        raise ValidationError(_("This text is invalid."), code=name)
    text = unicodedata.normalize("NFC", value.replace("\r\n", "\n")).strip()
    if any(unicodedata.category(c) == "Cc" and c not in _ALLOWED_CONTROLS for c in text):
        raise ValidationError(_("The text contains a control character."), code=name)
    if not minimum <= len(text) <= maximum:
        raise ValidationError(
            _("Between %(min)d and %(max)d characters.") % {"min": minimum, "max": maximum},
            code=name,
        )
    return text


def parse_hours(value: Any) -> Decimal:
    """Half hours from 0.5 to 300; « 12,5 » and « 12.5 » are the same."""
    if isinstance(value, Decimal):
        hours = value
    elif isinstance(value, int) and not isinstance(value, bool):
        hours = Decimal(value)
    elif isinstance(value, str):
        try:
            hours = Decimal(value.strip().replace(",", "."))
        except InvalidOperation:
            hours = Decimal("NaN")
    else:
        hours = Decimal("NaN")
    if not hours.is_finite() or not HOURS_MIN <= hours <= HOURS_MAX or (hours * 2) % 1:
        raise ValidationError(
            _("Hours: a multiple of half an hour, from 0.5 to 300."), code="hours"
        )
    return hours.quantize(Decimal("0.1"))


def _shifted(day: dt.date, years: int) -> dt.date:
    try:
        return day.replace(year=day.year + years)
    except ValueError:  # 29 February
        return day.replace(year=day.year + years, day=28)


def date_bounds(thesis: Thesis) -> tuple[dt.date, dt.date]:
    """From 12 months before the CIFRE start to 12 months after the planned end."""
    return _shifted(thesis.cifre_start_date, -1), _shifted(thesis.planned_end_date, 1)


def _clean_dates(thesis: Thesis, start: Any, end: Any) -> tuple[dt.date, dt.date]:
    for value in (start, end):
        if isinstance(value, dt.datetime) or not isinstance(value, dt.date):
            raise ValidationError(_("A date is required."), code="date")
    if start > end:
        raise ValidationError(_("The end comes before the start."), code="date")
    low, high = date_bounds(thesis)
    if start < low or end > high:
        raise ValidationError(
            _("Dates between %(low)s and %(high)s.")
            % {"low": low.strftime("%d/%m/%Y"), "high": high.strftime("%d/%m/%Y")},
            code="date",
        )
    return start, end


def _clean_category(value: Any) -> TrainingCategory:
    if value not in TrainingCategory.values:
        raise ValidationError(_("Unknown category."), code="category")
    return TrainingCategory(value)


def _clean_level(value: Any) -> ConfidentialityLevel:
    if not isinstance(value, ConfidentialityLevel):
        raise ValidationError(_("Unknown confidentiality level."), code="level")
    return value


def _clean_certificate(user: User, thesis: Thesis, value: Any) -> Document | None:
    if value is None:
        return None
    if not isinstance(value, Document) or value.thesis_id != thesis.pk:
        raise ValidationError(_("Unknown document."), code="certificate")
    if not can_access_document(user, value):
        raise ValidationError(_("Unknown document."), code="certificate")
    return value


def _clean(user: User, thesis: Thesis, values: dict[str, Any]) -> dict[str, Any]:
    cleaned: dict[str, Any] = {
        name: _clean_text(values.get(name, ""), name) for name in TRAINING_LIMITS
    }
    cleaned["category"] = _clean_category(values.get("category"))
    cleaned["hours"] = parse_hours(values.get("hours"))
    cleaned["start_date"], cleaned["end_date"] = _clean_dates(
        thesis, values.get("start_date"), values.get("end_date")
    )
    cleaned["certificate"] = _clean_certificate(user, thesis, values.get("certificate"))
    return cleaned


# =========================================================================== rights


def _deny(user: Any, operation: str, reason: str, **kwargs: Any) -> PermissionDenied:
    """PORTFOLIO_ACCESS_DENIED in its own transaction (kept although an exception follows)."""
    with transaction.atomic():
        audit(
            AuditAction.PORTFOLIO_ACCESS_DENIED,
            actor=user,
            operation=operation,
            reason=reason,
            **kwargs,
        )
    return PermissionDenied()


def _require(user: Any, action: Action, thesis: Thesis, operation: str) -> None:
    if not can(user, action, thesis):
        raise _deny(user, operation, "missing_right", thesis=thesis)


def _from_to(previous: Any, new: Any) -> dict[str, Any]:
    """Audit details ``from`` / ``to`` (``from`` is a keyword in Python)."""
    return {"from": previous, "to": new}


def _require_mfa(recent_mfa: bool) -> None:
    if not recent_mfa:
        raise ReauthenticationRequiredError()


def _fresh(thesis: Thesis) -> Thesis:
    return Thesis.objects.get(pk=thesis.pk)


def _frozen() -> ValidationError:
    return ValidationError(_("The thesis is finished: its trainings are frozen."), code="frozen")


def _check_open(thesis: Thesis, module: str = MODULE) -> None:
    if not is_open_for(_fresh(thesis), module):
        raise _frozen()


def _lock_thesis(thesis: Thesis, module: str = MODULE) -> Thesis:
    locked = Thesis.objects.select_for_update().get(pk=thesis.pk)
    if not is_open_for(locked, module):
        raise _frozen()
    return locked


def _partial(user: Any, thesis: Thesis) -> bool:
    """True when ``user`` does not read every level a training may have: the totals
    then say they count the trainings the user reads. A function of the roles only,
    never of the trainings (two worlds)."""
    return not set(ConfidentialityLevel) <= readable_levels(user, thesis)


# =========================================================================== reading


def training_for(user: User, thesis: Thesis, training_id: Any, *, operation: str) -> Training:
    """The training ``training_id`` of ``thesis`` if ``user`` reads it.

    Unknown, malformed, of another thesis than the URL's, or of a level the user does
    not read: the same audited PermissionDenied (the journal says why).
    """
    try:
        training = Training.objects.filter(pk=training_id, thesis=thesis).first()
    except (ValueError, ValidationError):
        training = None
    if training is None:
        raise _deny(user, operation, "unknown_training", thesis=thesis)
    _require(user, Action.TRAINING_READ, thesis, operation)
    if not sees(user, training, Action.TRAINING_READ):
        raise _deny(user, operation, "unreadable_level", thesis=thesis)
    return training


def _today() -> dt.date:
    return timezone.localdate()


def _is_done(training: Training, today: dt.date) -> bool:
    return training.end_date <= today


@dataclass(frozen=True)
class TrainingRow:
    training: Training
    title: str
    provider: str
    notes: str  # "" for whoever is not the student
    certificate: Document | None  # only when the reader reads it
    done: bool

    @property
    def internal(self) -> bool:
        return self.training.level != ConfidentialityLevel.PUBLIC


@dataclass(frozen=True)
class CategoryTotal:
    category: TrainingCategory
    hours: Decimal
    count: int


@dataclass(frozen=True)
class TrainingList:
    done: list[TrainingRow]
    upcoming: list[TrainingRow]
    totals: list[CategoryTotal]  # every category, in the order of the choices
    total_hours: Decimal
    integrity_done: bool
    partial: bool
    can_write: bool
    can_export: bool


def _texts(user: User, thesis: Thesis, trainings: list[Training]) -> dict[tuple[Any, str], Any]:
    private = can(user, Action.TRAINING_WRITE, thesis)
    names = [f.value for f in TEXT_FIELDS if private or f.value not in PRIVATE_TEXTS]
    return vault.open_texts(
        user, thesis, [(t, name) for t in trainings for name in names], action=Action.TRAINING_READ
    )


def _shown(value: Any) -> str:
    if value is vault.RESTRICTED:  # a bug upstream: refuse rather than hint
        raise PermissionDenied("a text of an unreadable training reached a page")
    return str(value or "")


def _rows(user: User, thesis: Thesis, trainings: list[Training]) -> list[TrainingRow]:
    texts = _texts(user, thesis, trainings)
    today = _today()
    rows = []
    for t in trainings:
        certificate = t.certificate if t.certificate_id is not None else None
        if certificate is not None and not can_access_document(user, certificate):
            certificate = None  # no signal of a document the reader does not read
        rows.append(
            TrainingRow(
                training=t,
                title=_shown(texts.get((t.pk, "title"), "")),
                provider=_shown(texts.get((t.pk, "provider"), "")),
                notes=_shown(texts.get((t.pk, "notes"), "")),
                certificate=certificate,
                done=_is_done(t, today),
            )
        )
    return rows


def totals_of(trainings: Iterable[Training], today: dt.date) -> list[CategoryTotal]:
    """Hours and counts per category of the FINISHED trainings given (the caller
    gives only the trainings the reader reads)."""
    hours = {c: Decimal("0") for c in TrainingCategory}
    counts = dict.fromkeys(TrainingCategory, 0)
    for t in trainings:
        if not _is_done(t, today):
            continue
        category = TrainingCategory(t.category)
        hours[category] += t.hours
        counts[category] += 1
    return [CategoryTotal(c, hours[c], counts[c]) for c in TrainingCategory]


def training_list(user: User, thesis: Thesis) -> TrainingList:
    """The trainings ``user`` reads, the finished ones first, and their totals."""
    _require(user, Action.TRAINING_READ, thesis, "list")
    trainings = list(
        Training.objects.readable_by(user, thesis)
        .select_related("certificate")
        .order_by("-end_date", "-start_date", "created_at", "pk")
    )
    rows = _rows(user, thesis, trainings)
    today = _today()
    totals = totals_of(trainings, today)
    return TrainingList(
        done=[r for r in rows if r.done],
        upcoming=sorted((r for r in rows if not r.done), key=lambda r: r.training.start_date),
        totals=totals,
        total_hours=sum((t.hours for t in totals), Decimal("0")),
        integrity_done=any(t.count for t in totals if t.category is TrainingCategory.INTEGRITY),
        partial=_partial(user, thesis),
        can_write=can(user, Action.TRAINING_WRITE, thesis),
        can_export=can(user, Action.PORTFOLIO_EXPORT, thesis),
    )


def training_detail(user: User, training: Training) -> TrainingRow:
    """One training, as its page shows it (the caller got it from training_for)."""
    thesis = _fresh(training.thesis)
    return _rows(user, thesis, [training])[0]


def attachable_documents(user: User, thesis: Thesis) -> list[Document]:
    """The documents of the thesis ``user`` may give as a certificate (reads them)."""
    candidates = Document.objects.filter(thesis=thesis).order_by("title", "pk")
    return [d for d in candidates if can_access_document(user, d)]


def policy_of(thesis: Thesis) -> PortfolioPolicy:
    """The settings of the thesis (an unsaved default when none were set)."""
    found = PortfolioPolicy.objects.filter(thesis=thesis).first()
    return found if found is not None else PortfolioPolicy(thesis=thesis)


# =========================================================================== writing


def create_training(
    *,
    thesis: Thesis,
    user: User,
    title: str,
    category: str,
    hours: Any,
    start_date: dt.date,
    end_date: dt.date,
    provider: str = "",
    notes: str = "",
    certificate: Document | None = None,
    level: ConfidentialityLevel = ConfidentialityLevel.PUBLIC,
) -> Training:
    _require(user, Action.TRAINING_WRITE, thesis, "create")
    thesis = _fresh(thesis)
    _check_open(thesis)
    level = _clean_level(level)
    cleaned = _clean(
        user,
        thesis,
        {
            "title": title, "provider": provider, "notes": notes, "category": category,
            "hours": hours, "start_date": start_date, "end_date": end_date,
            "certificate": certificate,
        },
    )  # fmt: skip
    with vault.sealing(
        user=user, thesis=thesis, level=level, action=Action.TRAINING_WRITE
    ) as sealer:
        _lock_thesis(thesis)
        training = Training(
            thesis=thesis,
            category=cleaned["category"].value,
            hours=cleaned["hours"],
            start_date=cleaned["start_date"],
            end_date=cleaned["end_date"],
            certificate=cleaned["certificate"],
            level=level.value,
            created_by=user,
        )
        training.save(force_insert=True)
        for field in TEXT_FIELDS:
            if cleaned[field.value]:
                sealer.seal(training, field.value, cleaned[field.value])
        audit(
            AuditAction.TRAINING_CREATED,
            actor=user,
            obj=training,
            category=training.category,
            level=training.level,
        )
    return training


def update_training(*, training: Training, user: User, values: dict[str, Any]) -> Training:
    """Replace the definition of a training (every field but the level and the ADUM
    state); only the changed fields are written and journaled (names only)."""
    thesis = _fresh(training.thesis)
    _require(user, Action.TRAINING_WRITE, thesis, "update")
    _check_open(thesis)
    fresh = Training.objects.get(pk=training.pk)
    cleaned = _clean(user, thesis, values)
    current = vault.open_texts(
        user, thesis, [(fresh, f.value) for f in TEXT_FIELDS], action=Action.TRAINING_WRITE
    )
    level = ConfidentialityLevel(fresh.level)
    with vault.sealing(user=user, thesis=thesis, level=level, action=Action.TRAINING_WRITE) as s:
        _lock_thesis(thesis)
        locked = Training.objects.select_for_update().get(pk=fresh.pk)
        if locked.level != fresh.level or locked.updated_at != fresh.updated_at:
            raise ValidationError(_("The training changed meanwhile: try again."), code="stale")
        changed = []
        certificate = cleaned["certificate"]
        clear = {
            "category": cleaned["category"].value,
            "hours": cleaned["hours"],
            "start_date": cleaned["start_date"],
            "end_date": cleaned["end_date"],
            "certificate_id": certificate.pk if certificate is not None else None,
        }
        for attr, new in clear.items():
            if getattr(locked, attr) != new:
                setattr(locked, attr, new)
                changed.append(attr.removesuffix("_id"))
        for field in TEXT_FIELDS:
            name = field.value
            if str(current.get((locked.pk, name), "") or "") == cleaned[name]:
                continue
            if cleaned[name]:
                s.seal(locked, name, cleaned[name])
            else:
                s.erase(locked, name)
            changed.append(name)
        if not changed:
            return locked
        locked.save()
        audit(AuditAction.TRAINING_UPDATED, actor=user, obj=locked, fields=sorted(changed))
    return locked


def change_training_level(
    *, training: Training, user: User, level: ConfidentialityLevel, recent_mfa: bool
) -> Training:
    """Move the training's texts to ``level`` (second factor)."""
    thesis = _fresh(training.thesis)
    _require(user, Action.TRAINING_WRITE, thesis, "level")
    _require_mfa(recent_mfa)
    level = _clean_level(level)
    _check_open(thesis)
    fresh = Training.objects.get(pk=training.pk)
    previous = fresh.level
    if level.value == previous:
        return fresh
    with vault.sealing(user=user, thesis=thesis, level=level, action=Action.TRAINING_WRITE) as s:
        _lock_thesis(thesis)
        locked = Training.objects.select_for_update().get(pk=fresh.pk)
        if locked.level != previous:
            raise ValidationError(_("The training changed meanwhile: try again."), code="stale")
        for field in TEXT_FIELDS:
            s.reseal(locked, field.value)
        locked.level = level.value
        locked.save()
        audit(
            AuditAction.TRAINING_LEVEL_CHANGED,
            actor=user,
            obj=locked,
            **_from_to(previous, level.value),
        )
    return locked


def set_adum_status(*, training: Training, user: User, status: str) -> Training:
    """The student says the training is (or is no longer) entered in ADUM."""
    thesis = _fresh(training.thesis)
    _require(user, Action.TRAINING_WRITE, thesis, "adum")
    if status not in AdumStatus.values:
        raise ValidationError(_("Unknown ADUM state."), code="adum_status")
    _check_open(thesis)
    with transaction.atomic():
        _lock_thesis(thesis)
        locked = Training.objects.select_for_update().get(pk=training.pk)
        if locked.adum_status == status:
            return locked
        locked.adum_status = status
        locked.save(update_fields=["adum_status", "updated_at"])
        audit(AuditAction.TRAINING_ADUM_STATUS_CHANGED, actor=user, obj=locked, to=status)
    return locked


def set_reregistration_date(*, thesis: Thesis, user: User, day: dt.date | None) -> PortfolioPolicy:
    """The date of the next re-registration (the reminders count from it); None clears it."""
    _require(user, Action.TRAINING_WRITE, thesis, "policy")
    thesis = _fresh(thesis)
    _check_open(thesis)
    if day is not None:
        if isinstance(day, dt.datetime) or not isinstance(day, dt.date):
            raise ValidationError(_("A date is required."), code="date")
        low, high = date_bounds(thesis)
        if not low <= day <= high:
            raise ValidationError(_("This date is outside the thesis."), code="date")
    with transaction.atomic():
        _lock_thesis(thesis)
        policy, _created = PortfolioPolicy.objects.select_for_update().get_or_create(
            thesis=thesis, defaults={"created_by": user}
        )
        previous = policy.reregistration_date
        if previous == day:
            return policy
        policy.reregistration_date = day
        policy.save()
        audit(
            AuditAction.PORTFOLIO_POLICY_CHANGED,
            actor=user,
            thesis=thesis,
            obj=policy,
            **_from_to(
                previous.isoformat() if previous else None, day.isoformat() if day else None
            ),
        )
    return policy


# ==================================================================== M9b: publications

PUBLICATIONS_MODULE = "portfolio_publications"  # apps/accounts/statuses.py
_DETAIL_RULES: dict[str, tuple[int, re.Pattern[str] | None]] = {
    "doi": (200, re.compile(r"10\.\d{4,9}/\S+")),
    "hal_id": (32, re.compile(r"[a-z]+-\d{8}(?:v\d+)?")),
    "volume": (32, None),
    "pages": (32, re.compile(r"[0-9A-Za-z]+(?:-{1,2}[0-9A-Za-z]+)?")),
    "bibtex_key": (64, re.compile(r"[A-Za-z0-9:_-]+")),
}
_BY_VENUE = {
    "JOURNAL": AdumCategory.JOURNAL_ARTICLE,
    "CONFERENCE": AdumCategory.CONFERENCE_PAPER,
    "WORKSHOP": AdumCategory.CONFERENCE_PAPER,
    "PHD_DAY": AdumCategory.ORAL_COMMUNICATION,
}


def suggested_category(venue_kind: str, presentation_kind: str) -> AdumCategory:
    """The ADUM category a submission gets until the student chooses one (ED7)."""
    if presentation_kind == "INVITED_TALK":
        return AdumCategory.INVITED_TALK
    if presentation_kind == "POSTER":
        return AdumCategory.POSTER
    if venue_kind == "SEMINAR":
        return AdumCategory.SEMINAR
    if presentation_kind == "TALK":
        return AdumCategory.ORAL_COMMUNICATION
    return _BY_VENUE.get(venue_kind, AdumCategory.OTHER)


def _clean_detail(value: Any, name: str) -> str:
    """One line, no control character, bounded, of a strict form."""
    limit, pattern = _DETAIL_RULES[name]
    if not isinstance(value, str):
        raise ValidationError(_("This text is invalid."), code=name)
    text = unicodedata.normalize("NFC", value).strip()
    if any(unicodedata.category(c)[0] in "CZ" and c != " " for c in text) or len(text) > limit:
        raise ValidationError(_("This value is invalid."), code=name)
    if text and pattern is not None and not pattern.fullmatch(text):
        raise ValidationError(_("This value is invalid."), code=name)
    return text


def _clean_details(thesis: Thesis, values: dict[str, Any]) -> dict[str, Any]:
    category = values.get("adum_category")
    if category not in AdumCategory.values:
        raise ValidationError(_("Unknown category."), code="adum_category")
    day = values.get("presented_on")
    if day is not None:
        low, high = date_bounds(thesis)
        if isinstance(day, dt.datetime) or not isinstance(day, dt.date) or not low <= day <= high:
            raise ValidationError(_("This date is outside the thesis."), code="presented_on")
    cleaned: dict[str, Any] = {"adum_category": category, "presented_on": day}
    for name in DETAILS_TEXTS:
        cleaned[name] = _clean_detail(values.get(name, ""), name)
    return cleaned


@dataclass(frozen=True)
class PublicationRow:
    submission: Submission
    title: str
    authors: list[str]  # the student, the internal co-authors, the external ones
    details: PublicationDetails | None
    texts: dict[str, str]  # DETAILS_TEXTS, "" when none
    category: str  # the details', or the suggested one
    date: dt.date | None  # presented on, else the Paris date of the publication

    @property
    def internal(self) -> bool:
        return self.submission.level != ConfidentialityLevel.PUBLIC

    @property
    def accepted(self) -> bool:
        """Accepted, not published yet: « à paraître »."""
        return self.submission.status == "ACCEPTED"

    @property
    def is_publication(self) -> bool:
        return bool(self.category in PUBLICATION_CATEGORIES)

    @property
    def category_label(self) -> str:
        return str(AdumCategory(self.category).label)


def publication_for(
    user: User, thesis: Thesis, submission_id: Any, *, operation: str
) -> Submission:
    """The submission of ``thesis`` whose details ``user`` reads: unknown, of another
    thesis or of a level the user does not read, the same audited refusal."""
    try:
        submission = Submission.objects.filter(pk=submission_id, thesis=thesis).first()
    except (ValueError, ValidationError):
        submission = None
    if submission is None:
        raise _deny(user, operation, "unknown_submission", thesis=thesis)
    _require(user, Action.SUBMISSION_READ, thesis, operation)
    if not sees(user, submission, Action.SUBMISSION_READ):
        raise _deny(user, operation, "unreadable_level", thesis=thesis)
    return submission


def _people(submissions: list[Submission]) -> dict[Any, list[str]]:
    from apps.accounts.models import Membership
    from apps.accounts.roles import Role
    from apps.conferences.models import SubmissionAuthor

    out: dict[Any, list[str]] = {s.pk: [] for s in submissions}
    if not submissions:
        return out
    thesis_id = submissions[0].thesis_id
    student = (
        Membership.objects.filter(thesis_id=thesis_id, role=Role.PHD_STUDENT)
        .select_related("user")
        .order_by("-active_from")
        .first()
    )
    authors = (
        SubmissionAuthor.objects.filter(submission__in=submissions)
        .select_related("user")
        .order_by("created_at", "pk")
    )
    for s in submissions:
        if student is not None:
            out[s.pk].append(student.user.get_full_name() or student.user.email)
    for a in authors:
        out[a.submission_id].append(a.user.get_full_name() or a.user.email)
    return out


def _publication_rows(user: User, thesis: Thesis, submissions: list[Submission]) -> list[Any]:
    details = {
        d.submission_id: d for d in PublicationDetails.objects.filter(submission__in=submissions)
    }
    items: list[tuple[Any, str]] = [
        (s, n) for s in submissions for n in ("title", "external_authors")
    ]
    items += [(d, n) for d in details.values() for n in DETAILS_TEXTS]
    opened = vault.open_texts(user, thesis, items, action=Action.SUBMISSION_READ)
    people = _people(submissions)
    rows = []
    for s in submissions:
        d = details.get(s.pk)
        external = _shown(opened.get((s.pk, "external_authors"), ""))
        texts = {n: _shown(opened.get((d.pk, n), "")) if d else "" for n in DETAILS_TEXTS}
        published = timezone.localtime(s.published_at).date() if s.published_at else None
        rows.append(
            PublicationRow(
                submission=s,
                title=_shown(opened.get((s.pk, "title"), "")),
                authors=people[s.pk] + [a.strip() for a in external.splitlines() if a.strip()],
                details=d,
                texts=texts,
                category=d.adum_category
                if d
                else suggested_category(s.venue.kind, s.presentation_kind).value,
                date=(d.presented_on if d and d.presented_on else None) or published,
            )
        )
    return rows


def publication_list(user: User, thesis: Thesis) -> list[PublicationRow]:
    """The submissions ``user`` reads, with their details (SR-01: no other one)."""
    _require(user, Action.SUBMISSION_READ, thesis, "publications")
    submissions = list(
        Submission.objects.readable_by(user, thesis)
        .select_related("venue", "edition")
        .order_by("-created_at", "pk")
    )
    return _publication_rows(user, thesis, submissions)


def publication_detail(user: User, submission: Submission) -> PublicationRow:
    thesis = _fresh(submission.thesis)
    _require(user, Action.SUBMISSION_READ, thesis, "publication")
    fresh = Submission.objects.select_related("venue", "edition").get(pk=submission.pk)
    if not sees(user, fresh, Action.SUBMISSION_READ):
        raise _deny(user, "publication", "unreadable_level", thesis=thesis)
    rows: list[PublicationRow] = _publication_rows(user, thesis, [fresh])
    return rows[0]


def set_publication_details(
    *, submission: Submission, user: User, values: dict[str, Any]
) -> PublicationDetails:
    """The details of a submission, written by the student, sealed at its level;
    only the changed fields are written and journaled (names only)."""
    thesis = _fresh(submission.thesis)
    _require(user, Action.SUBMISSION_WRITE, thesis, "publication")
    _check_open(thesis, PUBLICATIONS_MODULE)
    fresh = Submission.objects.get(pk=submission.pk)
    if fresh.thesis_id != thesis.pk or not sees(user, fresh, Action.SUBMISSION_READ):
        raise _deny(user, "publication", "unreadable_level", thesis=thesis)
    cleaned = _clean_details(thesis, values)
    existing = PublicationDetails.objects.filter(submission=fresh).first()
    current: dict[tuple[Any, str], Any] = {}
    if existing is not None:
        current = vault.open_texts(
            user, thesis, [(existing, n) for n in DETAILS_TEXTS], action=Action.SUBMISSION_READ
        )
    level = ConfidentialityLevel(fresh.level)
    with vault.sealing(user=user, thesis=thesis, level=level, action=Action.SUBMISSION_WRITE) as s:
        _lock_thesis(thesis, PUBLICATIONS_MODULE)
        locked = Submission.objects.select_for_update().get(pk=fresh.pk)
        if locked.level != fresh.level:
            raise ValidationError(_("The submission changed meanwhile: try again."), code="stale")
        details = PublicationDetails.objects.select_for_update().filter(submission=locked).first()
        if (details is None) != (existing is None):
            raise ValidationError(_("The details changed meanwhile: try again."), code="stale")
        changed: list[str] = []
        if details is None:
            details = PublicationDetails(
                thesis=thesis, submission=locked, adum_category=cleaned["adum_category"],
                presented_on=cleaned["presented_on"], created_by=user,
            )  # fmt: skip
            details.save(force_insert=True)
            changed.append("adum_category")
            if cleaned["presented_on"] is not None:
                changed.append("presented_on")
        else:
            for name in ("adum_category", "presented_on"):
                if getattr(details, name) != cleaned[name]:
                    setattr(details, name, cleaned[name])
                    changed.append(name)
        for name in DETAILS_TEXTS:
            if str(current.get((details.pk, name), "") or "") == cleaned[name]:
                continue
            if cleaned[name]:
                s.seal(details, name, cleaned[name])
            else:
                s.erase(details, name)
            changed.append(name)
        if not changed:
            return details
        details.save()
        audit(AuditAction.PUBLICATION_DETAILS_SET, actor=user, obj=locked, fields=sorted(changed))
    return details


# ==================================================================== M9b: teaching, outreach


@dataclass(frozen=True)
class _Spec:
    """What differs between a teaching and an outreach action."""

    model: Any
    read: Action
    write: Action
    texts: tuple[str, ...]
    clear: tuple[str, ...]  # the clear fields an update may change
    created: AuditAction
    updated: AuditAction
    level_changed: AuditAction
    unknown: str  # the journal's reason for an unknown id
    stale: str


TEACHING = _Spec(
    Teaching, Action.TEACHING_READ, Action.TEACHING_WRITE, TEACHING_TEXTS,
    ("kind", "hours", "start_date", "end_date"), AuditAction.TEACHING_CREATED,
    AuditAction.TEACHING_UPDATED, AuditAction.TEACHING_LEVEL_CHANGED, "unknown_teaching",
    "The teaching changed meanwhile: try again.",
)  # fmt: skip
OUTREACH = _Spec(
    Outreach, Action.OUTREACH_READ, Action.OUTREACH_WRITE, OUTREACH_TEXTS, ("kind", "date"),
    AuditAction.OUTREACH_CREATED, AuditAction.OUTREACH_UPDATED,
    AuditAction.OUTREACH_LEVEL_CHANGED, "unknown_outreach",
    "The outreach action changed meanwhile: try again.",
)  # fmt: skip


def _clean_teaching(thesis: Thesis, values: dict[str, Any]) -> dict[str, Any]:
    cleaned: dict[str, Any] = {n: _clean_text(values.get(n, ""), n) for n in TEACHING_TEXTS}
    if values.get("kind") not in TeachingKind.values:
        raise ValidationError(_("Unknown type."), code="kind")
    cleaned["kind"] = values["kind"]
    cleaned["hours"] = parse_hours(values.get("hours"))
    cleaned["start_date"], cleaned["end_date"] = _clean_dates(
        thesis, values.get("start_date"), values.get("end_date")
    )
    return cleaned


def _clean_outreach(thesis: Thesis, values: dict[str, Any]) -> dict[str, Any]:
    cleaned: dict[str, Any] = {n: _clean_text(values.get(n, ""), n) for n in OUTREACH_TEXTS}
    if values.get("kind") not in OutreachKind.values:
        raise ValidationError(_("Unknown type."), code="kind")
    cleaned["kind"] = values["kind"]
    cleaned["date"], _end = _clean_dates(thesis, values.get("date"), values.get("date"))
    return cleaned


def _end_of(obj: Any) -> dt.date:
    return obj.end_date if isinstance(obj, Teaching) else obj.date


def _start_of(obj: Any) -> dt.date:
    return obj.start_date if isinstance(obj, Teaching) else obj.date


@dataclass(frozen=True)
class ActivityRow:
    """A teaching or an outreach action, as a page shows it."""

    obj: Any
    texts: dict[str, str]  # "notes" is "" for whoever is not the student
    done: bool

    @property
    def main(self) -> str:
        """The course of a teaching, the title of an outreach action."""
        return self.texts.get("course") or self.texts.get("title") or ""

    @property
    def second(self) -> str:
        return self.texts.get("institution") or self.texts.get("audience") or ""

    @property
    def teaching(self) -> Any:
        return self.obj

    @property
    def outreach(self) -> Any:
        return self.obj

    @property
    def internal(self) -> bool:
        return bool(self.obj.level != ConfidentialityLevel.PUBLIC)


@dataclass(frozen=True)
class KindTotal:
    kind: str
    label: str
    hours: Decimal
    count: int


@dataclass(frozen=True)
class ActivityList:
    done: list[ActivityRow]
    upcoming: list[ActivityRow]
    totals: list[KindTotal]  # every type, in the order of the choices
    total_hours: Decimal
    partial: bool
    can_write: bool


def _activity_for(spec: _Spec, user: User, thesis: Thesis, item_id: Any, operation: str) -> Any:
    try:
        obj = spec.model.objects.filter(pk=item_id, thesis=thesis).first()
    except (ValueError, ValidationError):
        obj = None
    if obj is None:
        raise _deny(user, operation, spec.unknown, thesis=thesis)
    _require(user, spec.read, thesis, operation)
    if not sees(user, obj, spec.read):
        raise _deny(user, operation, "unreadable_level", thesis=thesis)
    return obj


def _activity_rows(spec: _Spec, user: User, thesis: Thesis, objs: list[Any]) -> list[ActivityRow]:
    private = can(user, spec.write, thesis)
    names = [n for n in spec.texts if private or n != "notes"]
    opened = vault.open_texts(user, thesis, [(o, n) for o in objs for n in names], action=spec.read)
    today = _today()
    return [
        ActivityRow(
            obj=o,
            texts={n: _shown(opened.get((o.pk, n), "")) if n in names else "" for n in spec.texts},
            done=_end_of(o) <= today,
        )
        for o in objs
    ]


def _activity_list(spec: _Spec, kinds: Any, user: User, thesis: Thesis) -> ActivityList:
    _require(user, spec.read, thesis, "list")
    objs = list(
        spec.model.objects.readable_by(user, thesis).order_by(*spec.model._meta.ordering, "pk")
    )
    rows = _activity_rows(spec, user, thesis, objs)
    hours = {k: Decimal("0") for k in kinds}
    counts = dict.fromkeys(kinds, 0)
    for row in rows:
        if row.done:
            kind = kinds(row.obj.kind)
            hours[kind] += getattr(row.obj, "hours", Decimal("0"))
            counts[kind] += 1
    totals = [KindTotal(k.value, str(k.label), hours[k], counts[k]) for k in kinds]
    return ActivityList(
        done=[r for r in rows if r.done],
        upcoming=sorted((r for r in rows if not r.done), key=lambda r: _start_of(r.obj)),
        totals=totals,
        total_hours=sum((t.hours for t in totals), Decimal("0")),
        partial=_partial(user, thesis),
        can_write=can(user, spec.write, thesis),
    )


def _create_activity(
    spec: _Spec, *, thesis: Thesis, user: User, level: Any, cleaned: dict[str, Any]
) -> Any:
    level = _clean_level(level)
    with vault.sealing(user=user, thesis=thesis, level=level, action=spec.write) as sealer:
        _lock_thesis(thesis)
        clear = {k: v for k, v in cleaned.items() if k not in spec.texts}
        obj = spec.model(thesis=thesis, level=level.value, created_by=user, **clear)
        obj.save(force_insert=True)
        for name in spec.texts:
            if cleaned[name]:
                sealer.seal(obj, name, cleaned[name])
        audit(spec.created, actor=user, obj=obj, kind=obj.kind, level=obj.level)
    return obj


def _update_activity(spec: _Spec, *, obj: Any, user: User, cleaned: dict[str, Any]) -> Any:
    thesis = _fresh(obj.thesis)
    fresh = spec.model.objects.get(pk=obj.pk)
    current = vault.open_texts(user, thesis, [(fresh, n) for n in spec.texts], action=spec.write)
    level = ConfidentialityLevel(fresh.level)
    with vault.sealing(user=user, thesis=thesis, level=level, action=spec.write) as s:
        _lock_thesis(thesis)
        locked = spec.model.objects.select_for_update().get(pk=fresh.pk)
        if locked.level != fresh.level or locked.updated_at != fresh.updated_at:
            raise ValidationError(_(spec.stale), code="stale")
        changed = []
        for name in spec.clear:
            if getattr(locked, name) != cleaned[name]:
                setattr(locked, name, cleaned[name])
                changed.append(name)
        for name in spec.texts:
            if str(current.get((locked.pk, name), "") or "") == cleaned[name]:
                continue
            if cleaned[name]:
                s.seal(locked, name, cleaned[name])
            else:
                s.erase(locked, name)
            changed.append(name)
        if not changed:
            return locked
        locked.save()
        audit(spec.updated, actor=user, obj=locked, fields=sorted(changed))
    return locked


def _change_activity_level(
    spec: _Spec, *, obj: Any, user: User, level: Any, recent_mfa: bool
) -> Any:
    thesis = _fresh(obj.thesis)
    _require(user, spec.write, thesis, "level")
    _require_mfa(recent_mfa)
    level = _clean_level(level)
    _check_open(thesis)
    fresh = spec.model.objects.get(pk=obj.pk)
    previous = fresh.level
    if level.value == previous:
        return fresh
    with vault.sealing(user=user, thesis=thesis, level=level, action=spec.write) as s:
        _lock_thesis(thesis)
        locked = spec.model.objects.select_for_update().get(pk=fresh.pk)
        if locked.level != previous:
            raise ValidationError(_(spec.stale), code="stale")
        for name in spec.texts:
            s.reseal(locked, name)
        locked.level = level.value
        locked.save()
        audit(spec.level_changed, actor=user, obj=locked, **_from_to(previous, level.value))
    return locked


def teaching_for(user: User, thesis: Thesis, teaching_id: Any, *, operation: str) -> Teaching:
    obj: Teaching = _activity_for(TEACHING, user, thesis, teaching_id, operation)
    return obj


def teaching_list(user: User, thesis: Thesis) -> ActivityList:
    return _activity_list(TEACHING, TeachingKind, user, thesis)


def teaching_detail(user: User, teaching: Teaching) -> ActivityRow:
    thesis = _fresh(teaching.thesis)
    _require(user, Action.TEACHING_READ, thesis, "detail")
    return _activity_rows(TEACHING, user, thesis, [teaching])[0]


def create_teaching(
    *,
    thesis: Thesis,
    user: User,
    course: str,
    kind: str,
    hours: Any,
    start_date: dt.date,
    end_date: dt.date,
    institution: str = "",
    notes: str = "",
    level: ConfidentialityLevel = ConfidentialityLevel.PUBLIC,
) -> Teaching:
    _require(user, Action.TEACHING_WRITE, thesis, "create")
    thesis = _fresh(thesis)
    _check_open(thesis)
    cleaned = _clean_teaching(thesis, {
        "course": course, "institution": institution, "notes": notes, "kind": kind,
        "hours": hours, "start_date": start_date, "end_date": end_date,
    })  # fmt: skip
    obj: Teaching = _create_activity(TEACHING, thesis=thesis, user=user, level=level,
                                     cleaned=cleaned)  # fmt: skip
    return obj


def update_teaching(*, teaching: Teaching, user: User, values: dict[str, Any]) -> Teaching:
    thesis = _fresh(teaching.thesis)
    _require(user, Action.TEACHING_WRITE, thesis, "update")
    _check_open(thesis)
    cleaned = _clean_teaching(thesis, values)
    obj: Teaching = _update_activity(TEACHING, obj=teaching, user=user, cleaned=cleaned)
    return obj


def change_teaching_level(
    *, teaching: Teaching, user: User, level: ConfidentialityLevel, recent_mfa: bool
) -> Teaching:
    obj: Teaching = _change_activity_level(
        TEACHING, obj=teaching, user=user, level=level, recent_mfa=recent_mfa
    )
    return obj


def outreach_for(user: User, thesis: Thesis, outreach_id: Any, *, operation: str) -> Outreach:
    obj: Outreach = _activity_for(OUTREACH, user, thesis, outreach_id, operation)
    return obj


def outreach_list(user: User, thesis: Thesis) -> ActivityList:
    return _activity_list(OUTREACH, OutreachKind, user, thesis)


def outreach_detail(user: User, outreach: Outreach) -> ActivityRow:
    thesis = _fresh(outreach.thesis)
    _require(user, Action.OUTREACH_READ, thesis, "detail")
    return _activity_rows(OUTREACH, user, thesis, [outreach])[0]


def create_outreach(
    *,
    thesis: Thesis,
    user: User,
    title: str,
    kind: str,
    date: dt.date,
    audience: str = "",
    notes: str = "",
    level: ConfidentialityLevel = ConfidentialityLevel.PUBLIC,
) -> Outreach:
    _require(user, Action.OUTREACH_WRITE, thesis, "create")
    thesis = _fresh(thesis)
    _check_open(thesis)
    cleaned = _clean_outreach(thesis, {"title": title, "audience": audience, "notes": notes,
                                       "kind": kind, "date": date})  # fmt: skip
    obj: Outreach = _create_activity(OUTREACH, thesis=thesis, user=user, level=level,
                                     cleaned=cleaned)  # fmt: skip
    return obj


def update_outreach(*, outreach: Outreach, user: User, values: dict[str, Any]) -> Outreach:
    thesis = _fresh(outreach.thesis)
    _require(user, Action.OUTREACH_WRITE, thesis, "update")
    _check_open(thesis)
    cleaned = _clean_outreach(thesis, values)
    obj: Outreach = _update_activity(OUTREACH, obj=outreach, user=user, cleaned=cleaned)
    return obj


def change_outreach_level(
    *, outreach: Outreach, user: User, level: ConfidentialityLevel, recent_mfa: bool
) -> Outreach:
    obj: Outreach = _change_activity_level(
        OUTREACH, obj=outreach, user=user, level=level, recent_mfa=recent_mfa
    )
    return obj


# ==================================================================== M9b: the student notes


@dataclass(frozen=True)
class SkillRow:
    skill: SkillNote
    texts: dict[str, str]


def _personal(user: Any, action: Action, thesis: Thesis, operation: str) -> None:
    _require(user, action, thesis, operation)


def _skill_texts(user: User, thesis: Thesis, skills: list[SkillNote]) -> list[SkillRow]:
    opened = vault.open_texts(
        user, thesis, [(k, n) for k in skills for n in SKILL_TEXTS], action=Action.CAREER_NOTE_READ
    )
    return [
        SkillRow(k, {n: _shown(opened.get((k.pk, n), "")) for n in SKILL_TEXTS}) for k in skills
    ]


def skill_list(user: User, thesis: Thesis) -> list[SkillRow]:
    _personal(user, Action.CAREER_NOTE_READ, thesis, "skills")
    skills = list(
        SkillNote.objects.readable_by(user, thesis).order_by("category", "created_at", "pk")
    )
    return _skill_texts(user, thesis, skills)


def skill_for(user: User, thesis: Thesis, skill_id: Any, *, operation: str) -> SkillNote:
    try:
        skill = SkillNote.objects.filter(pk=skill_id, thesis=thesis).first()
    except (ValueError, ValidationError):
        skill = None
    if skill is None:
        raise _deny(user, operation, "unknown_skill", thesis=thesis)
    _personal(user, Action.CAREER_NOTE_READ, thesis, operation)
    return skill


def skill_detail(user: User, skill: SkillNote) -> SkillRow:
    thesis = _fresh(skill.thesis)
    _personal(user, Action.CAREER_NOTE_READ, thesis, "skill")
    return _skill_texts(user, thesis, [skill])[0]


def _clean_skill(values: dict[str, Any]) -> dict[str, Any]:
    cleaned: dict[str, Any] = {n: _clean_text(values.get(n, ""), n) for n in SKILL_TEXTS}
    if values.get("category") not in SkillCategory.values:
        raise ValidationError(_("Unknown category."), code="category")
    cleaned["category"] = values["category"]
    return cleaned


def create_skill(
    *, thesis: Thesis, user: User, title: str, category: str, evidence: str = ""
) -> SkillNote:
    _personal(user, Action.CAREER_NOTE_WRITE, thesis, "create_skill")
    thesis = _fresh(thesis)
    _check_open(thesis)
    cleaned = _clean_skill({"title": title, "evidence": evidence, "category": category})
    with vault.sealing(
        user=user, thesis=thesis, level=PERSONAL_LEVEL, action=Action.CAREER_NOTE_WRITE
    ) as sealer:
        _lock_thesis(thesis)
        skill = SkillNote(thesis=thesis, category=cleaned["category"], created_by=user)
        skill.save(force_insert=True)
        for name in SKILL_TEXTS:
            if cleaned[name]:
                sealer.seal(skill, name, cleaned[name])
        audit(AuditAction.SKILL_NOTE_CREATED, actor=user, obj=skill, category=skill.category)
    return skill


def update_skill(*, skill: SkillNote, user: User, values: dict[str, Any]) -> SkillNote:
    thesis = _fresh(skill.thesis)
    _personal(user, Action.CAREER_NOTE_WRITE, thesis, "update_skill")
    _check_open(thesis)
    cleaned = _clean_skill(values)
    current = vault.open_texts(
        user, thesis, [(skill, n) for n in SKILL_TEXTS], action=Action.CAREER_NOTE_WRITE
    )
    with vault.sealing(
        user=user, thesis=thesis, level=PERSONAL_LEVEL, action=Action.CAREER_NOTE_WRITE
    ) as s:
        _lock_thesis(thesis)
        locked = SkillNote.objects.select_for_update().get(pk=skill.pk)
        changed = []
        if locked.category != cleaned["category"]:
            locked.category = cleaned["category"]
            changed.append("category")
        for name in SKILL_TEXTS:
            if str(current.get((locked.pk, name), "") or "") == cleaned[name]:
                continue
            if cleaned[name]:
                s.seal(locked, name, cleaned[name])
            else:
                s.erase(locked, name)
            changed.append(name)
        if not changed:
            return locked
        locked.save()
        audit(AuditAction.SKILL_NOTE_UPDATED, actor=user, obj=locked, fields=sorted(changed))
    return locked


def career_note(user: User, thesis: Thesis) -> str:
    """The professional project of the student ("" when none)."""
    _personal(user, Action.CAREER_NOTE_READ, thesis, "career")
    note = CareerNote.objects.readable_by(user, thesis).first()
    if note is None:
        return ""
    opened = vault.open_texts(user, thesis, [(note, "text")], action=Action.CAREER_NOTE_READ)
    return _shown(opened.get((note.pk, "text"), ""))


def set_career_note(*, thesis: Thesis, user: User, text: str) -> CareerNote | None:
    """Replace the professional project; "" erases its text (the row stays)."""
    _personal(user, Action.CAREER_NOTE_WRITE, thesis, "set_career")
    thesis = _fresh(thesis)
    _check_open(thesis)
    cleaned = _clean_text(text, "text")
    existing = CareerNote.objects.filter(thesis=thesis).first()
    if existing is None and not cleaned:
        return None
    if existing is not None and career_note(user, thesis) == cleaned:
        return existing
    with vault.sealing(
        user=user, thesis=thesis, level=PERSONAL_LEVEL, action=Action.CAREER_NOTE_WRITE
    ) as s:
        _lock_thesis(thesis)
        note, _created = CareerNote.objects.select_for_update().get_or_create(
            thesis=thesis, defaults={"created_by": user}
        )
        if cleaned:
            s.seal(note, "text", cleaned)
        else:
            s.erase(note, "text")
        note.save()
        audit(AuditAction.CAREER_NOTE_CHANGED, actor=user, obj=note)
    return note


# =========================================================================== ADUM summary

# The sections of the summary and of its CSV (the student's notes are never in it).
ADUM_SECTIONS = ("trainings", "publications", "communications", "teaching", "outreach")
_CSV_NAMES = {"trainings": "formations", "publications": "publications",
              "communications": "communications", "teaching": "enseignements",
              "outreach": "diffusion"}  # fmt: skip


@dataclass(frozen=True)
class AdumSummary:
    year: Any  # objectives.ThesisYear
    years: list[Any]
    rows: list[TrainingRow]
    include_internal: bool
    left_out: int  # internal trainings of the year not shown (include_internal off)
    hours: Decimal
    # M9b
    publications: list[PublicationRow]
    communications: list[PublicationRow]
    teachings: list[ActivityRow]
    outreach: list[ActivityRow]
    include_accepted: bool
    left_out_other: int  # internal items of the other sections not shown
    accepted_left_out: int  # accepted submissions of the year not shown
    publishable: bool  # a published (or accepted) submission the reader reads: BibTeX


def _years(thesis: Thesis) -> list[Any]:
    from apps.objectives.models import ThesisYear

    return list(ThesisYear.objects.filter(thesis=thesis).order_by("number"))


def _year(thesis: Thesis, number: int | None) -> Any:
    from apps.objectives.services import current_academic_year

    years = _years(thesis)
    if number is not None:
        for year in years:
            if year.number == number:
                return year
        raise ValidationError(_("Unknown year."), code="year")
    return current_academic_year(thesis) or (years[-1] if years else None)


def _in_year(day: dt.date | None, year: Any) -> bool:
    return day is not None and year.academic_start <= day <= year.academic_end


def _published(user: User, thesis: Thesis, *, include_accepted: bool) -> list[PublicationRow]:
    """The published submissions ``user`` reads (and the accepted ones, on request),
    in the order of their date."""
    statuses = ["PUBLISHED", "ACCEPTED"] if include_accepted else ["PUBLISHED"]
    submissions = list(
        Submission.objects.readable_by(user, thesis)
        .filter(status__in=statuses)
        .select_related("venue", "edition")
        .order_by("created_at", "pk")
    )
    rows: list[PublicationRow] = _publication_rows(user, thesis, submissions)
    far = dt.date.max
    return sorted(rows, key=lambda r: (r.date or far, r.submission.created_at))


def _publications_of_year(user: User, thesis: Thesis, year: Any) -> list[PublicationRow]:
    """Published (dated) in the year; accepted ones by their date presented, or in the
    current academic year when they have none (« à paraître »)."""
    today = _today()
    current = _in_year(today, year)
    return [
        r
        for r in _published(user, thesis, include_accepted=True)
        if _in_year(r.date, year) or (r.accepted and r.date is None and current)
    ]


def adum_summary(
    user: User,
    thesis: Thesis,
    *,
    year: int | None = None,
    include_internal: bool = False,
    include_accepted: bool = False,
) -> AdumSummary:
    """The items of the academic year, as ADUM wants them (columns to be confirmed,
    ED6, ED7): finished trainings, teaching and outreach; published submissions (the
    accepted ones on request, « à paraître »). PUBLIC only unless
    ``include_internal``: entering an item in ADUM discloses it outside the platform."""
    _require(user, Action.PORTFOLIO_EXPORT, thesis, "adum")
    chosen = _year(thesis, year)
    if chosen is None:
        raise ValidationError(_("Unknown year."), code="year")
    today = _today()
    of_year = [
        t
        for t in Training.objects.readable_by(user, thesis)
        .filter(end_date__gte=chosen.academic_start, end_date__lte=chosen.academic_end)
        .select_related("certificate")
        .order_by("start_date", "end_date", "created_at", "pk")
        if _is_done(t, today)
    ]
    shown = [t for t in of_year if include_internal or t.level == ConfidentialityLevel.PUBLIC]
    rows = _rows(user, thesis, shown)

    def allowed(internal: bool) -> bool:
        return include_internal or not internal

    papers = _publications_of_year(user, thesis, chosen)
    by_status = [r for r in papers if include_accepted or not r.accepted]
    accepted_left_out = len([r for r in papers if r.accepted and allowed(r.internal)]) - len(
        [r for r in by_status if r.accepted and allowed(r.internal)]
    )
    papers_shown = [r for r in by_status if allowed(r.internal)]
    activities: dict[str, list[ActivityRow]] = {}
    for key, spec in (("teaching", TEACHING), ("outreach", OUTREACH)):
        objs = [
            o
            for o in spec.model.objects.readable_by(user, thesis).order_by("created_at", "pk")
            if _end_of(o) <= today and _in_year(_end_of(o), chosen)
        ]
        activities[key] = sorted(_activity_rows(spec, user, thesis, objs),
                                 key=lambda r: (_start_of(r.obj), r.obj.created_at))  # fmt: skip
    every_other = by_status + activities["teaching"] + activities["outreach"]
    left_out_other = len([r for r in every_other if not allowed(r.internal)])
    return AdumSummary(
        year=chosen,
        years=_years(thesis),
        rows=rows,
        include_internal=include_internal,
        left_out=len(of_year) - len(shown),
        hours=sum((t.hours for t in shown), Decimal("0")),
        publications=[r for r in papers_shown if r.is_publication],
        communications=[r for r in papers_shown if not r.is_publication],
        teachings=[r for r in activities["teaching"] if allowed(r.internal)],
        outreach=[r for r in activities["outreach"] if allowed(r.internal)],
        include_accepted=include_accepted,
        left_out_other=left_out_other,
        accepted_left_out=accepted_left_out,
        publishable=Submission.objects.readable_by(user, thesis)
        .filter(status__in=["PUBLISHED", "ACCEPTED"])
        .exists(),
    )


def neutralized(value: str) -> str:
    """A cell a spreadsheet will not run: an apostrophe before a formula start."""
    return f"'{value}" if value.startswith(FORMULA_STARTS) else value


def _hours_text(hours: Decimal) -> str:
    text = format(hours.normalize(), "f")
    return text.replace(".", ",")


def _day_text(day: dt.date | None) -> str:
    return day.strftime("%d/%m/%Y") if day else ""


def _level_text(level: str) -> str:
    return str(ConfidentialityLevel(level).label)


# Literal calls: makemessages reads them (a variable would leave them untranslated).
_COLUMNS = {
    "Title": pgettext_lazy("ADUM column", "Title"),
    "Provider": pgettext_lazy("ADUM column", "Provider"),
    "Category": pgettext_lazy("ADUM column", "Category"),
    "Hours": pgettext_lazy("ADUM column", "Hours"),
    "Start": pgettext_lazy("ADUM column", "Start"),
    "End": pgettext_lazy("ADUM column", "End"),
    "Level": pgettext_lazy("ADUM column", "Level"),
    "Authors": pgettext_lazy("ADUM column", "Authors"),
    "Venue": pgettext_lazy("ADUM column", "Venue"),
    "Date": pgettext_lazy("ADUM column", "Date"),
    "Volume": pgettext_lazy("ADUM column", "Volume"),
    "Pages": pgettext_lazy("ADUM column", "Pages"),
    "DOI": pgettext_lazy("ADUM column", "DOI"),
    "HAL": pgettext_lazy("ADUM column", "HAL"),
    "Status": pgettext_lazy("ADUM column", "Status"),
    "Course": pgettext_lazy("ADUM column", "Course"),
    "Institution": pgettext_lazy("ADUM column", "Institution"),
    "Type": pgettext_lazy("ADUM column", "Type"),
    "Audience": pgettext_lazy("ADUM column", "Audience"),
}


def _csv_rows(section: str, summary: AdumSummary) -> tuple[list[str], list[list[str]]]:
    """(header, rows) of one section; every typed text is neutralized."""

    def col(name: str) -> str:
        return str(_COLUMNS[name])

    n = neutralized
    if section == "trainings":
        return (
            [col("Title"), col("Provider"), col("Category"), col("Hours"), col("Start"),
             col("End"), col("Level")],
            [[n(r.title), n(r.provider), str(TrainingCategory(r.training.category).label),
              _hours_text(r.training.hours), _day_text(r.training.start_date),
              _day_text(r.training.end_date), _level_text(r.training.level)]
             for r in summary.rows],
        )  # fmt: skip
    if section in ("publications", "communications"):
        rows = summary.publications if section == "publications" else summary.communications
        return (
            [col("Authors"), col("Title"), col("Venue"), col("Date"), col("Volume"),
             col("Pages"), col("DOI"), col("HAL"), col("Category"), col("Status"),
             col("Level")],
            [[n(", ".join(r.authors)), n(r.title),
              n(f"{r.submission.venue.acronym} — {r.submission.venue.name}"), _day_text(r.date),
              n(r.texts["volume"]), n(r.texts["pages"]), n(r.texts["doi"]),
              n(r.texts["hal_id"]), r.category_label,
              pgettext("ADUM status", "to appear") if r.accepted else "",
              _level_text(r.submission.level)]
             for r in rows],
        )  # fmt: skip
    if section == "teaching":
        return (
            [col("Course"), col("Institution"), col("Type"), col("Hours"), col("Start"),
             col("End"), col("Level")],
            [[n(r.texts["course"]), n(r.texts["institution"]),
              str(TeachingKind(r.obj.kind).label), _hours_text(r.obj.hours),
              _day_text(r.obj.start_date), _day_text(r.obj.end_date), _level_text(r.obj.level)]
             for r in summary.teachings],
        )  # fmt: skip
    return (
        [col("Title"), col("Audience"), col("Type"), col("Date"), col("Level")],
        [[n(r.texts["title"]), n(r.texts["audience"]), str(OutreachKind(r.obj.kind).label),
          _day_text(r.obj.date), _level_text(r.obj.level)]
         for r in summary.outreach],
    )  # fmt: skip


def adum_csv(
    *,
    thesis: Thesis,
    user: User,
    year: int | None,
    include_internal: bool,
    recent_mfa: bool,
    section: str = "trainings",
    include_accepted: bool = False,
) -> tuple[str, bytes]:
    """(file name, content) of the CSV of one section of the ADUM summary; second
    factor, journaled with numbers only."""
    _require(user, Action.PORTFOLIO_EXPORT, thesis, "adum_csv")
    _require_mfa(recent_mfa)
    if section not in ADUM_SECTIONS:
        raise ValidationError(_("Unknown section."), code="section")
    summary = adum_summary(user, thesis, year=year, include_internal=include_internal,
                           include_accepted=include_accepted)  # fmt: skip
    header, rows = _csv_rows(section, summary)
    buffer = io.StringIO()
    writer = csv.writer(buffer, delimiter=";", lineterminator="\r\n", quoting=csv.QUOTE_MINIMAL)
    writer.writerow(header)
    writer.writerows(rows)
    chosen = summary.year
    details: dict[str, Any] = {"year": chosen.number, "rows": len(rows),
                               "internal": include_internal}  # fmt: skip
    if section != "trainings":  # M9a's line is unchanged
        details.update(section=section, accepted=include_accepted)
    with transaction.atomic():
        audit(AuditAction.PORTFOLIO_EXPORTED, actor=user, thesis=thesis, **details)
    start, end = chosen.academic_start.year, chosen.academic_end.year
    name = f"{_CSV_NAMES[section]}-adum-{start}-{end}.csv"
    return name, (BOM + buffer.getvalue()).encode("utf-8")


# =========================================================================== BibTeX

_BIBTEX_ESCAPES = {
    "\\": r"\textbackslash{}", "{": r"\{", "}": r"\}", "$": r"\$", "&": r"\&", "%": r"\%",
    "#": r"\#", "_": r"\_", "~": r"\textasciitilde{}", "^": r"\textasciicircum{}",
}  # fmt: skip
_BIBTEX_TYPES = {"JOURNAL_ARTICLE": "article", "CONFERENCE_PAPER": "inproceedings"}


def _bibtex_value(text: str) -> str:
    """A field value BibTeX and LaTeX read as text only: every special character
    escaped (no brace, no command survives), control and format characters (the
    bidirectional ones included) removed, one line."""
    out = []
    for char in unicodedata.normalize("NFC", text):
        if char in "\n\r\t":
            out.append(" ")
        elif unicodedata.category(char)[0] == "C" or unicodedata.category(char) in ("Zl", "Zp"):
            continue
        else:
            out.append(_BIBTEX_ESCAPES.get(char, char))
    return " ".join("".join(out).split())


def _bibtex_entry(row: PublicationRow, key: str) -> str:
    kind = _BIBTEX_TYPES.get(row.category, "misc")
    venue = row.submission.venue
    where = {"article": "journal", "inproceedings": "booktitle"}.get(kind, "howpublished")
    fields: list[tuple[str, str]] = [
        ("author", " and ".join(row.authors)),
        ("title", row.title),
        (where, venue.name),
        ("year", str(row.date.year) if row.date else ""),
        ("volume", row.texts["volume"]),
        ("pages", row.texts["pages"]),
        ("doi", row.texts["doi"]),
        ("note", f"HAL: {row.texts['hal_id']}" if row.texts["hal_id"] else ""),
    ]
    lines = [f"  {name} = {{{_bibtex_value(value)}}}" for name, value in fields if value]
    return f"@{kind}{{{key},\n" + ",\n".join(lines) + "\n}"


def publications_bibtex(
    *,
    thesis: Thesis,
    user: User,
    include_internal: bool,
    include_accepted: bool,
    recent_mfa: bool,
) -> tuple[str, bytes]:
    """(file name, content) of the BibTeX of the published submissions (the accepted
    ones on request); PUBLIC only by default; second factor, journaled in numbers."""
    _require(user, Action.PORTFOLIO_EXPORT, thesis, "bibtex")
    _require_mfa(recent_mfa)
    rows = [
        r
        for r in _published(user, thesis, include_accepted=include_accepted)
        if include_internal or not r.internal
    ]
    used: set[str] = set()
    entries = []
    for n, row in enumerate(rows, start=1):
        base = row.texts["bibtex_key"] or f"pub-{n}"
        key, suffix = base, 1
        while key in used:
            suffix += 1
            key = f"{base}-{suffix}"
        used.add(key)
        entries.append(_bibtex_entry(row, key))
    with transaction.atomic():
        audit(AuditAction.PORTFOLIO_EXPORTED, actor=user, thesis=thesis, section="bibtex",
              rows=len(rows), internal=include_internal, accepted=include_accepted)  # fmt: skip
    return "publications.bib", ("\n\n".join(entries) + "\n").encode("utf-8")
