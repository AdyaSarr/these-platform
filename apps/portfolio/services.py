"""Business logic of the portfolio (M9a): trainings, the ADUM summary, the date of
the next re-registration (docs/modules/M09-portfolio.md).

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
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.utils import timezone
from django.utils.translation import gettext as _
from django.utils.translation import pgettext

from apps.accounts.models import Thesis, User
from apps.accounts.permissions import Action, can, can_access_document, readable_levels, sees
from apps.accounts.statuses import is_open_for
from apps.core.confidentiality import ConfidentialityLevel
from apps.vault import services as vault
from apps.vault.audit import AuditAction, audit
from apps.vault.errors import ReauthenticationRequiredError
from apps.vault.models import Document

from .models import (
    HOURS_MAX,
    HOURS_MIN,
    TEXT_FIELDS,
    AdumStatus,
    PortfolioPolicy,
    Training,
    TrainingCategory,
)

MODULE = "portfolio"  # apps/accounts/statuses.py
TEXT_LIMITS = {"title": (1, 200), "provider": (0, 200), "notes": (0, 2000)}
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
        name: _clean_text(values.get(name, ""), name) for name in TEXT_LIMITS
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


def _check_open(thesis: Thesis) -> None:
    if not is_open_for(_fresh(thesis), MODULE):
        raise _frozen()


def _lock_thesis(thesis: Thesis) -> Thesis:
    locked = Thesis.objects.select_for_update().get(pk=thesis.pk)
    if not is_open_for(locked, MODULE):
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


# =========================================================================== ADUM summary


@dataclass(frozen=True)
class AdumSummary:
    year: Any  # objectives.ThesisYear
    years: list[Any]
    rows: list[TrainingRow]
    include_internal: bool
    left_out: int  # internal trainings of the year not shown (include_internal off)
    hours: Decimal


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


def adum_summary(
    user: User, thesis: Thesis, *, year: int | None = None, include_internal: bool = False
) -> AdumSummary:
    """The finished trainings whose end falls in the academic year, as ADUM wants them
    (columns to be confirmed, ED6). PUBLIC only unless ``include_internal``: entering
    a training in ADUM discloses it outside the platform."""
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
    return AdumSummary(
        year=chosen,
        years=_years(thesis),
        rows=rows,
        include_internal=include_internal,
        left_out=len(of_year) - len(shown),
        hours=sum((t.hours for t in shown), Decimal("0")),
    )


def neutralized(value: str) -> str:
    """A cell a spreadsheet will not run: an apostrophe before a formula start."""
    return f"'{value}" if value.startswith(FORMULA_STARTS) else value


def _hours_text(hours: Decimal) -> str:
    text = format(hours.normalize(), "f")
    return text.replace(".", ",")


def adum_csv(
    *,
    thesis: Thesis,
    user: User,
    year: int | None,
    include_internal: bool,
    recent_mfa: bool,
) -> tuple[str, bytes]:
    """(file name, content) of the CSV of the ADUM summary; second factor, journaled."""
    _require(user, Action.PORTFOLIO_EXPORT, thesis, "adum_csv")
    _require_mfa(recent_mfa)
    summary = adum_summary(user, thesis, year=year, include_internal=include_internal)
    buffer = io.StringIO()
    writer = csv.writer(buffer, delimiter=";", lineterminator="\r\n", quoting=csv.QUOTE_MINIMAL)
    writer.writerow(
        [
            pgettext("ADUM column", "Title"),
            pgettext("ADUM column", "Provider"),
            pgettext("ADUM column", "Category"),
            pgettext("ADUM column", "Hours"),
            pgettext("ADUM column", "Start"),
            pgettext("ADUM column", "End"),
            pgettext("ADUM column", "Level"),
        ]
    )
    for row in summary.rows:
        t = row.training
        writer.writerow(
            [
                neutralized(row.title),
                neutralized(row.provider),
                str(TrainingCategory(t.category).label),
                _hours_text(t.hours),
                t.start_date.strftime("%d/%m/%Y"),
                t.end_date.strftime("%d/%m/%Y"),
                str(ConfidentialityLevel(t.level).label),
            ]
        )
    chosen = summary.year
    with transaction.atomic():
        audit(
            AuditAction.PORTFOLIO_EXPORTED,
            actor=user,
            thesis=thesis,
            year=chosen.number,
            rows=len(summary.rows),
            internal=include_internal,
        )
    name = f"formations-adum-{chosen.academic_start.year}-{chosen.academic_end.year}.csv"
    return name, (BOM + buffer.getvalue()).encode("utf-8")
