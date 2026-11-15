"""Business logic of the travel module (M7): presence calendar, counters, settings.

Rights first (permissions.can, refusals journaled), then the second factor when
the action needs one, then validation, then one transaction that locks the thesis
row and ends with audit(). What a viewer sees of the presence calendar is filtered
by access.py before anything is counted (data minimisation).
"""

from __future__ import annotations

import calendar
import datetime as dt
import unicodedata
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from fractions import Fraction
from typing import Any

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.utils import timezone
from django.utils.translation import gettext as _

from apps.accounts.models import Membership, Thesis, User
from apps.accounts.permissions import Action, can, can_access_level, roles_of
from apps.accounts.roles import Role
from apps.conferences.models import Edition, Submission
from apps.core.confidentiality import ConfidentialityLevel
from apps.meetings.models import Meeting
from apps.meetings.timing import meeting_bounds
from apps.objectives.years import cifre_bounds, cifre_year_of, year_count
from apps.vault import formats
from apps.vault import services as vault
from apps.vault.audit import AuditAction
from apps.vault.errors import ReauthenticationRequiredError
from apps.vault.models import Document
from apps.vault.services import audit

from . import amounts, holidays, split
from .access import PresenceAccess, presence_access
from .choices import (
    PRESENCE_OF_DESTINATION,
    Destination,
    Lodging,
    MissionStatus,
    OrderDecision,
    OrderRefusalReason,
    Payer,
    PresenceKind,
    ShownKind,
    Side,
    Transport,
)
from .models import (
    DEFAULT_LEVEL,
    MAX_GAP_ALERT_POINTS,
    MAX_LAB_DAYS_PER_MONTH,
    MAX_MISSION_DAYS,
    MAX_ORDER_LEAD_DAYS,
    MAX_RECEIPT_GRACE_DAYS,
    ORDER_PAYERS,
    PAYER_LEVELS,
    Mission,
    MissionOrder,
    PresenceDay,
    TimeSplitTarget,
    TravelPolicy,
)

OPEN_THESIS = (Thesis.Status.SETUP, Thesis.Status.ACTIVE)
MAX_RANGE_DAYS = 92  # one quarter in a single request
_ONE_DAY = dt.timedelta(days=1)


# =========================================================================== helpers


def _from_to(previous: Any, new: Any) -> dict[str, Any]:
    """Audit details ``from`` / ``to`` (``from`` is a keyword in Python)."""
    return {"from": previous, "to": new}


def _deny(
    user: Any, operation: str, reason: str, *, thesis: Thesis, **details: Any
) -> PermissionDenied:
    with transaction.atomic():
        audit(
            AuditAction.TRAVEL_ACCESS_DENIED,
            actor=user,
            thesis=thesis,
            operation=operation,
            reason=reason,
            **details,
        )
    return PermissionDenied()


def _require(user: Any, action: Action, thesis: Thesis, operation: str) -> None:
    if not can(user, action, thesis):
        raise _deny(user, operation, "missing_right", thesis=thesis)


def _require_mfa(recent_mfa: bool) -> None:
    if not recent_mfa:
        raise ReauthenticationRequiredError()


def _frozen() -> ValidationError:
    return ValidationError(_("The thesis is finished: its travel data is frozen."), code="frozen")


def _lock_thesis(thesis_id: Any) -> Thesis:
    thesis = Thesis.objects.select_for_update().get(pk=thesis_id)
    if thesis.status not in OPEN_THESIS:
        raise _frozen()
    return thesis


def _fresh(thesis: Thesis) -> Thesis:
    return Thesis.objects.get(pk=thesis.pk)


def _int_in(value: Any, low: int, high: int, message: str, code: str) -> int:
    if type(value) is not int or not low <= value <= high:  # bool and float refused
        raise ValidationError(message, code=code)
    return value


def bounds(thesis: Thesis) -> tuple[dt.date, dt.date]:
    """First and last date (inclusive) of the travel data of a thesis (same bounds
    as the meetings)."""
    return meeting_bounds(thesis.cifre_start_date, thesis.planned_end_date)


def _check_in_bounds(thesis: Thesis, first: dt.date, last: dt.date) -> None:
    low, high = bounds(thesis)
    if first < low or last > high:
        raise ValidationError(
            _("The dates must be between %(first)s and %(last)s.")
            % {"first": low.strftime("%d/%m/%Y"), "last": high.strftime("%d/%m/%Y")},
            code="out_of_bounds",
        )


def policy_of(thesis: Thesis) -> TravelPolicy:
    """The policy of the thesis, or an unsaved one with the defaults."""
    return TravelPolicy.objects.filter(thesis_id=thesis.pk).first() or TravelPolicy(
        thesis_id=thesis.pk
    )


def rules_of(policy: TravelPolicy) -> split.CountingRules:
    return split.CountingRules(
        remote_counts_as=Side(policy.remote_counts_as),
        conference_counts_as=Side(policy.conference_counts_as),
        whit_monday_holiday=policy.whit_monday_holiday,
    )


def _require_reader(user: Any, thesis: Thesis, operation: str) -> PresenceAccess:
    access = presence_access(user, thesis)
    if access is PresenceAccess.NONE:
        raise _deny(user, operation, "missing_right", thesis=thesis)
    return access


# =========================================================================== presence


def set_presence(
    *,
    thesis: Thesis,
    user: User,
    first: dt.date,
    last: dt.date,
    kind: str | None,
    include_non_working: bool = False,
) -> int:
    """Set (or clear, ``kind=None``) the days of [first, last]; the working days only
    unless ``include_non_working``. A past day (before today in Paris) changed is
    journaled (PRESENCE_CORRECTED, one event per day). Returns the days changed."""
    _require(user, Action.TRAVEL_WRITE, thesis, "presence")
    holidays.require_date(first, "first")
    holidays.require_date(last, "last")
    if kind is not None and kind not in PresenceKind.values:
        raise ValidationError(_("Unknown kind of day."), code="kind")
    if first > last:
        raise ValidationError(_("The first day must not be after the last one."), code="range")
    if (last - first).days + 1 > MAX_RANGE_DAYS:
        raise ValidationError(
            _("At most %(n)d days at once.") % {"n": MAX_RANGE_DAYS}, code="too_long"
        )
    _check_in_bounds(_fresh(thesis), first, last)
    with transaction.atomic():
        locked = _lock_thesis(thesis.pk)
        if include_non_working:
            days = [first + n * _ONE_DAY for n in range((last - first).days + 1)]
        else:
            whit = policy_of(locked).whit_monday_holiday
            days = holidays.working_days(first, last, whit_monday=whit)
        changed, corrections = _write_presence(locked, user, days, kind, overwrite=True)
        _audit_corrections(locked, user, corrections)
    return changed


Correction = tuple[dt.date, str | None, str | None]  # day, from, to


def _write_presence(
    locked: Thesis, user: User, days: list[dt.date], kind: str | None, *, overwrite: bool
) -> tuple[int, list[Correction]]:
    """Inside the caller's transaction (thesis row locked): set ``days`` to ``kind``
    (None clears), keeping days already filled in unless ``overwrite``. Returns the
    count changed and the past days changed, to be journaled LAST by the caller."""
    today = timezone.localdate()
    existing = {
        row.day: row
        for row in PresenceDay.objects.select_for_update().filter(thesis=locked, day__in=days)
    }
    now = timezone.now()
    created: list[PresenceDay] = []
    updated: list[PresenceDay] = []
    deleted: list[Any] = []
    corrections: list[Correction] = []
    for day in days:
        row = existing.get(day)
        old = row.kind if row is not None else None
        if old == kind or (row is not None and not overwrite):
            continue
        if kind is None:
            if row is not None:
                deleted.append(row.pk)
        elif row is None:
            created.append(PresenceDay(thesis=locked, day=day, kind=kind, created_by=user))
        else:
            row.kind, row.updated_at = kind, now
            updated.append(row)
        if day < today:
            corrections.append((day, old, kind))
    PresenceDay.objects.bulk_create(created)
    PresenceDay.objects.bulk_update(updated, ["kind", "updated_at"])
    PresenceDay.objects.filter(pk__in=deleted).delete()
    return len(created) + len(updated) + len(deleted), corrections


def _audit_corrections(locked: Thesis, user: User, corrections: list[Correction]) -> None:
    for day, old, new in corrections:
        audit(
            AuditAction.PRESENCE_CORRECTED,
            actor=user,
            thesis=locked,
            day=day.isoformat(),
            **_from_to(old, new),
        )


def _shown(kind: str, access: PresenceAccess) -> str:
    if kind == PresenceKind.LEAVE and access is not PresenceAccess.FULL:
        return ShownKind.UNAVAILABLE.value
    return kind


def visible_days(user: Any, thesis: Thesis, first: dt.date, last: dt.date) -> dict[dt.date, str]:
    """{day: ShownKind} of [first, last] as ``user`` may see it (access.py)."""
    access = _require_reader(user, thesis, "presence_read")
    rows = (
        PresenceDay.objects.visible_to(user, thesis)
        .filter(day__gte=first, day__lte=last)
        .values_list("day", "kind")
    )
    return {day: _shown(kind, access) for day, kind in rows}


# =========================================================================== counters


@dataclass(frozen=True)
class SplitSummary:
    """The counter of one CIFRE year as one viewer may see it. ``None`` = not shown
    to this viewer (the co-supervisor gets the LIRMM days only)."""

    access: PresenceAccess
    cifre_year: int
    year_count: int
    first: dt.date
    last: dt.date  # up to today for the current year
    lirmm_days: int
    shown_counts: dict[str, int] = field(default_factory=dict)
    st_days: int | None = None
    lab_days: int | None = None
    st_percent: Fraction | None = None
    target_st_percent: int | None = None
    gap: Fraction | None = None
    alert: bool | None = None
    working_days: int | None = None
    unfilled_days: int | None = None


def _current_year(thesis: Thesis, today: dt.date, count: int) -> int:
    number = cifre_year_of(thesis.cifre_start_date, today)
    return 1 if number is None else min(number, count)


def split_summary(user: Any, thesis: Thesis, *, cifre_year: int | None = None) -> SplitSummary:
    access = _require_reader(user, thesis, "split_read")
    thesis = _fresh(thesis)
    count_years = year_count(thesis.cifre_start_date, thesis.planned_end_date)
    today = timezone.localdate()
    number = _current_year(thesis, today, count_years) if cifre_year is None else cifre_year
    _int_in(number, 1, count_years, _("Unknown CIFRE year."), "year")
    first, year_last = cifre_bounds(thesis.cifre_start_date, number)
    last = min(year_last, today)
    policy = policy_of(thesis)
    rules = rules_of(policy)
    rows = dict(
        PresenceDay.objects.visible_to(user, thesis)
        .filter(day__gte=first, day__lte=year_last)
        .values_list("day", "kind")
    )
    if last < first:  # a year still to come
        counted = split.SplitCount(first, last, 0, 0, 0, 0)
    else:
        counted = split.count(rows, first, last, rules)
    lirmm = counted.by_kind.get(PresenceKind.LIRMM, 0)
    if access is PresenceAccess.LIRMM_ONLY:
        # Only LIRMM rows were fetched: nothing else exists for this viewer.
        return SplitSummary(
            access=access,
            cifre_year=number,
            year_count=count_years,
            first=first,
            last=last,
            lirmm_days=lirmm,
            shown_counts={PresenceKind.LIRMM.value: lirmm} if lirmm else {},
        )
    shown: dict[str, int] = {}
    for kind, n in counted.by_kind.items():
        key = _shown(kind, access)
        shown[key] = shown.get(key, 0) + n
    target = (
        TimeSplitTarget.objects.filter(thesis=thesis, cifre_year=number)
        .values_list("st_percent", flat=True)
        .first()
    )
    gap = None if target is None else split.gap_points(counted, target_st_percent=target)
    return SplitSummary(
        access=access,
        cifre_year=number,
        year_count=count_years,
        first=first,
        last=last,
        lirmm_days=lirmm,
        shown_counts=shown,
        st_days=counted.st_days,
        lab_days=counted.lab_days,
        st_percent=counted.st_percent,
        target_st_percent=target,
        gap=gap,
        alert=split.is_alert(gap, threshold=policy.gap_alert_points),
        working_days=counted.working_days,
        unfilled_days=counted.unfilled_days,
    )


# =========================================================================== month view


@dataclass(frozen=True)
class Cell:
    day: dt.date
    kind: str | None  # ShownKind, None when blank (or hidden from this viewer)
    working: bool
    holiday: bool
    today: bool


@dataclass(frozen=True)
class MonthView:
    year: int
    month: int
    weeks: list[list[Cell | None]]
    previous: tuple[int, int] | None
    next: tuple[int, int] | None
    lirmm_days: int
    rule_minimum: int | None
    rule_met: bool | None
    access: PresenceAccess


def _month_in_bounds(thesis: Thesis, year: int, month: int) -> bool:
    low, high = bounds(thesis)
    return (low.year, low.month) <= (year, month) <= (high.year, high.month)


def _shift(year: int, month: int, step: int) -> tuple[int, int]:
    index = year * 12 + (month - 1) + step
    return index // 12, index % 12 + 1


def month_view(user: Any, thesis: Thesis, year: int, month: int) -> MonthView:
    access = _require_reader(user, thesis, "presence_read")
    thesis = _fresh(thesis)
    _int_in(month, 1, 12, _("Unknown month."), "month")
    if type(year) is not int or not _month_in_bounds(thesis, year, month):
        raise ValidationError(_("This month is outside the thesis."), code="out_of_bounds")
    first = dt.date(year, month, 1)
    last = dt.date(year, month, calendar.monthrange(year, month)[1])
    days = visible_days(user, thesis, first, last)
    policy = policy_of(thesis)
    whit = policy.whit_monday_holiday
    off = holidays.public_holidays(year, whit_monday=whit)
    today = timezone.localdate()
    weeks = [
        [
            Cell(
                day=day,
                kind=days.get(day),
                working=holidays.is_working_day(day, whit_monday=whit),
                holiday=day in off,
                today=day == today,
            )
            if day.month == month
            else None
            for day in week
        ]
        for week in calendar.Calendar(firstweekday=calendar.MONDAY).monthdatescalendar(year, month)
    ]
    lirmm_only = {d: k for d, k in days.items() if k == PresenceKind.LIRMM}
    lirmm = split.month_lab_days(lirmm_only, year, month, rules_of(policy))
    previous, following = _shift(year, month, -1), _shift(year, month, 1)
    return MonthView(
        year=year,
        month=month,
        weeks=weeks,
        previous=previous if _month_in_bounds(thesis, *previous) else None,
        next=following if _month_in_bounds(thesis, *following) else None,
        lirmm_days=lirmm,
        rule_minimum=policy.min_lab_days_per_month,
        rule_met=split.month_rule_met(lirmm, policy.min_lab_days_per_month),
        access=access,
    )


# =========================================================================== settings

_UNSET: Any = object()


def _changes(policy: TravelPolicy, values: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        name: _from_to(getattr(policy, name), value)
        for name, value in values.items()
        if getattr(policy, name) != value
    }


def _save_policy(
    thesis: Thesis, user: User, values: dict[str, Any], action: AuditAction
) -> TravelPolicy:
    with transaction.atomic():
        locked = _lock_thesis(thesis.pk)
        policy, _created = TravelPolicy.objects.select_for_update().get_or_create(
            thesis=locked, defaults={"created_by": user}
        )
        changes = _changes(policy, values)
        if not changes:
            return policy
        for name, value in values.items():
            setattr(policy, name, value)
        policy.save()
        audit(action, actor=user, obj=policy, thesis=locked, changes=changes)
    return policy


def set_rules(
    *,
    thesis: Thesis,
    user: User,
    recent_mfa: bool,
    remote_counts_as: str = _UNSET,
    conference_counts_as: str = _UNSET,
    whit_monday_holiday: bool = _UNSET,
    gap_alert_points: int = _UNSET,
    min_lab_days_per_month: int | None = _UNSET,
) -> TravelPolicy:
    """Counting rules: the director or the tutor; an argument left out is unchanged."""
    _require(user, Action.TRAVEL_RULES_SET, thesis, "rules")
    _require_mfa(recent_mfa)
    given = {
        "remote_counts_as": remote_counts_as,
        "conference_counts_as": conference_counts_as,
        "whit_monday_holiday": whit_monday_holiday,
        "gap_alert_points": gap_alert_points,
        "min_lab_days_per_month": min_lab_days_per_month,
    }
    values = {name: value for name, value in given.items() if value is not _UNSET}
    for name in ("remote_counts_as", "conference_counts_as"):
        if name in values and values[name] not in Side.values:
            raise ValidationError(_("Unknown side."), code="side")
    if "whit_monday_holiday" in values and type(values["whit_monday_holiday"]) is not bool:
        raise ValidationError(_("Yes or no."), code="whit_monday")
    if "gap_alert_points" in values:
        _int_in(
            values["gap_alert_points"],
            1,
            MAX_GAP_ALERT_POINTS,
            _("Between 1 and %(max)d points.") % {"max": MAX_GAP_ALERT_POINTS},
            "gap",
        )
    if values.get("min_lab_days_per_month") is not None:
        _int_in(
            values["min_lab_days_per_month"],
            0,
            MAX_LAB_DAYS_PER_MONTH,
            _("Between 0 and %(max)d days.") % {"max": MAX_LAB_DAYS_PER_MONTH},
            "month_rule",
        )
    return _save_policy(thesis, user, values, AuditAction.TRAVEL_RULES_CHANGED)


def set_reminders(
    *,
    thesis: Thesis,
    user: User,
    recent_mfa: bool,
    order_lead_days: int,
    receipt_grace_days: int,
) -> TravelPolicy:
    """Reminder periods: the student."""
    _require(user, Action.TRAVEL_REMINDERS_SET, thesis, "reminders")
    _require_mfa(recent_mfa)
    _int_in(
        order_lead_days,
        0,
        MAX_ORDER_LEAD_DAYS,
        _("Between 0 and %(max)d days.") % {"max": MAX_ORDER_LEAD_DAYS},
        "order_lead_days",
    )
    _int_in(
        receipt_grace_days,
        0,
        MAX_RECEIPT_GRACE_DAYS,
        _("Between 0 and %(max)d days.") % {"max": MAX_RECEIPT_GRACE_DAYS},
        "receipt_grace_days",
    )
    values = {"order_lead_days": order_lead_days, "receipt_grace_days": receipt_grace_days}
    return _save_policy(thesis, user, values, AuditAction.TRAVEL_REMINDERS_CHANGED)


def set_target(
    *, thesis: Thesis, user: User, recent_mfa: bool, cifre_year: int, st_percent: int
) -> TimeSplitTarget:
    """ST / lab target of one CIFRE year: the director or the tutor."""
    _require(user, Action.TRAVEL_RULES_SET, thesis, "target")
    _require_mfa(recent_mfa)
    fresh = _fresh(thesis)
    count = year_count(fresh.cifre_start_date, fresh.planned_end_date)
    _int_in(cifre_year, 1, count, _("Unknown CIFRE year."), "year")
    _int_in(st_percent, 0, 100, _("Between 0 and 100."), "percent")
    with transaction.atomic():
        locked = _lock_thesis(thesis.pk)
        target = (
            TimeSplitTarget.objects.select_for_update()
            .filter(thesis=locked, cifre_year=cifre_year)
            .first()
        )
        previous = None if target is None else target.st_percent
        if target is not None and previous == st_percent:
            return target
        if target is None:
            target = TimeSplitTarget(thesis=locked, cifre_year=cifre_year, created_by=user)
        target.st_percent, target.lab_percent = st_percent, 100 - st_percent
        target.save()
        audit(
            AuditAction.TRAVEL_TARGET_CHANGED,
            actor=user,
            obj=target,
            thesis=locked,
            cifre_year=cifre_year,
            **_from_to(previous, st_percent),
        )
    return target


def targets_of(thesis: Thesis) -> dict[int, int]:
    """{CIFRE year: ST percent} of the targets set."""
    return dict(
        TimeSplitTarget.objects.filter(thesis=thesis).values_list("cifre_year", "st_percent")
    )


# =========================================================================== missions

MISSION_TEXTS = ("destination_detail", "purpose", "lodging_note")
TEXT_LIMITS = {"destination_detail": 200, "purpose": 2000, "lodging_note": 500, "comment": 2000}
ONE_LINE = frozenset({"destination_detail"})
MISSION_FIELDS = (
    "start_date",
    "end_date",
    "destination",
    "payer",
    "transports",
    "lodging",
    "edition",
    "meeting",
    "submission",
    "tickets_booked",
    "lodging_booked",
    "official_order_obtained",
)
_ALLOWED_CONTROLS = frozenset("\n\t")
_OPEN_MISSION = (
    MissionStatus.PLANNED,
    MissionStatus.ORDER_REQUESTED,
    MissionStatus.ORDER_GRANTED,
    MissionStatus.ORDER_REFUSED,
)


def _clean_text(value: Any, name: str) -> str:
    """NFC, CRLF -> LF, stripped; no control character but newline and tab; bounded."""
    if not isinstance(value, str):
        raise ValidationError(_("This text is invalid."), code=name)
    text = unicodedata.normalize("NFC", value.replace("\r\n", "\n")).strip()
    if any(unicodedata.category(c) == "Cc" and c not in _ALLOWED_CONTROLS for c in text):
        raise ValidationError(_("The text contains a control character."), code=name)
    if len(text) > TEXT_LIMITS[name] or len(text.encode()) > formats.MAX_TEXT_BYTES:
        raise ValidationError(
            _("At most %(max)d characters.") % {"max": TEXT_LIMITS[name]}, code=name
        )
    if name in ONE_LINE and ("\n" in text or "\t" in text):
        raise ValidationError(_("One line only."), code=name)
    return text


def _clean_level(level: Any) -> ConfidentialityLevel:
    if not isinstance(level, ConfidentialityLevel):
        raise ValidationError(_("Unknown confidentiality level."), code="level")
    return level


def _check_payer_level(payer: str, level: ConfidentialityLevel) -> None:
    allowed = PAYER_LEVELS.get(Payer(payer))
    if allowed is not None and level.value not in allowed:
        raise ValidationError(
            _("The person who decides for this payer must be able to read the mission."),
            code="payer_level",
        )


def _same_thesis(obj: Any, thesis: Thesis, code: str) -> None:
    if obj is not None and getattr(obj, "thesis_id", None) != thesis.pk:
        raise ValidationError(_("Unknown linked object."), code=code)


def _check_mission(thesis: Thesis, values: dict[str, Any]) -> None:
    """Every field of a mission, as it will be stored (``values`` complete)."""
    start, end = values["start_date"], values["end_date"]
    holidays.require_date(start, "start_date")
    holidays.require_date(end, "end_date")
    if start > end:
        raise ValidationError(_("The return must not be before the departure."), code="range")
    if (end - start).days + 1 > MAX_MISSION_DAYS:
        raise ValidationError(_("At most %(n)d days.") % {"n": MAX_MISSION_DAYS}, code="too_long")
    _check_in_bounds(thesis, start, end)
    if values["destination"] not in Destination.values:
        raise ValidationError(_("Unknown destination."), code="destination")
    if values["payer"] not in Payer.values:
        raise ValidationError(_("Unknown payer."), code="payer")
    if values["lodging"] not in Lodging.values:
        raise ValidationError(_("Unknown lodging."), code="lodging")
    transports = values["transports"]
    if (
        not isinstance(transports, list | tuple)
        or not transports
        or any(t not in Transport.values for t in transports)
        or len(set(transports)) != len(transports)
    ):
        raise ValidationError(_("Choose one or more means of transport."), code="transports")
    other = values["destination"] == Destination.OTHER
    if other and not values["destination_detail"]:
        raise ValidationError(_("Name the place (a town, a site)."), code="destination_detail")
    if not other and values["destination_detail"]:
        raise ValidationError(
            _("A place is named for another destination only."), code="destination_detail"
        )
    edition = values["edition"]
    if edition is not None and values["destination"] != Destination.CONFERENCE:
        raise ValidationError(_("A conference only for a conference trip."), code="edition")
    _same_thesis(edition, thesis, "edition")
    _same_thesis(values["meeting"], thesis, "meeting")
    _same_thesis(values["submission"], thesis, "submission")
    for name in ("tickets_booked", "lodging_booked", "official_order_obtained"):
        if type(values[name]) is not bool:
            raise ValidationError(_("Yes or no."), code=name)


def _check_open(thesis: Thesis) -> None:
    if _fresh(thesis).status not in OPEN_THESIS:
        raise _frozen()


def _mission_days(locked: Thesis, mission: Mission) -> list[dt.date]:
    whit = policy_of(locked).whit_monday_holiday
    return holidays.working_days(mission.start_date, mission.end_date, whit_monday=whit)


def create_mission(
    *,
    thesis: Thesis,
    user: User,
    start_date: dt.date,
    end_date: dt.date,
    destination: str,
    payer: str,
    level: ConfidentialityLevel | None = None,
    transports: list[str] | tuple[str, ...] = (Transport.TRAIN.value,),
    lodging: str = Lodging.NONE.value,
    destination_detail: str = "",
    purpose: str = "",
    lodging_note: str = "",
    edition: Any = None,
    meeting: Any = None,
    submission: Any = None,
    tickets_booked: bool = False,
    lodging_booked: bool = False,
    official_order_obtained: bool = False,
    fill_presence: bool = True,
) -> Mission:
    """A mission of the student; ``fill_presence`` fills its working days in the
    calendar (the kind of its destination), never over a day already filled in."""
    _require(user, Action.TRAVEL_WRITE, thesis, "mission_create")
    _check_open(thesis)
    if payer not in Payer.values:
        raise ValidationError(_("Unknown payer."), code="payer")
    level = DEFAULT_LEVEL[Payer(payer)] if level is None else _clean_level(level)
    texts = {
        "destination_detail": _clean_text(destination_detail, "destination_detail"),
        "purpose": _clean_text(purpose, "purpose"),
        "lodging_note": _clean_text(lodging_note, "lodging_note"),
    }
    values: dict[str, Any] = {
        "start_date": start_date,
        "end_date": end_date,
        "destination": destination,
        "payer": payer,
        "transports": list(transports) if isinstance(transports, list | tuple) else transports,
        "lodging": lodging,
        "edition": edition,
        "meeting": meeting,
        "submission": submission,
        "tickets_booked": tickets_booked,
        "lodging_booked": lodging_booked,
        "official_order_obtained": official_order_obtained,
        **texts,
    }
    _check_mission(_fresh(thesis), values)
    _check_payer_level(payer, level)
    with vault.sealing(user=user, thesis=thesis, level=level, action=Action.TRAVEL_WRITE) as sealer:
        locked = _lock_thesis(thesis.pk)
        mission = Mission(
            thesis=locked,
            level=level.value,
            created_by=user,
            **{name: values[name] for name in MISSION_FIELDS},
        )
        mission.save(force_insert=True)
        for name, text in texts.items():
            if text:
                sealer.seal(mission, name, text)
        corrections: list[Correction] = []
        kind = PRESENCE_OF_DESTINATION.get(Destination(destination))
        if fill_presence and kind is not None:
            _changed, corrections = _write_presence(
                locked, user, _mission_days(locked, mission), kind.value, overwrite=False
            )
        audit(
            AuditAction.MISSION_CREATED,
            actor=user,
            obj=mission,
            destination=mission.destination,
            payer=mission.payer,
            level=mission.level,
            start_date=mission.start_date.isoformat(),
            end_date=mission.end_date.isoformat(),
        )
        _audit_corrections(locked, user, corrections)
    return mission


def _locked_mission(mission: Mission) -> Mission:
    _lock_thesis(mission.thesis_id)
    return Mission.objects.select_for_update().get(pk=mission.pk)


def _terminal() -> ValidationError:
    return ValidationError(_("This mission is closed."), code="terminal")


def update_mission(*, mission: Mission, user: User, changes: Mapping[str, Any]) -> list[str]:
    """Fields and texts of an open mission (not its level, nor its status)."""
    unknown = sorted(set(changes) - {*MISSION_FIELDS, *MISSION_TEXTS})
    if unknown:
        raise ValueError(f"{unknown} cannot be changed here")
    thesis = _fresh(mission.thesis)
    _require(user, Action.TRAVEL_WRITE, thesis, "mission_update")
    _check_open(thesis)
    fresh = Mission.objects.get(pk=mission.pk)
    if fresh.is_closed:
        raise _terminal()
    current_texts = mission_texts(user, fresh)
    texts = {n: _clean_text(changes[n], n) for n in MISSION_TEXTS if n in changes}
    values: dict[str, Any] = {name: getattr(fresh, name) for name in MISSION_FIELDS}
    values.update({k: v for k, v in changes.items() if k in MISSION_FIELDS})
    if isinstance(values["transports"], tuple):
        values["transports"] = list(values["transports"])
    frozen = sorted(n for n in ORDER_SNAPSHOT_FIELDS if values[n] != getattr(fresh, n))
    if frozen and fresh.status == MissionStatus.ORDER_REQUESTED:
        # Before any other check: whatever the new values, the order is pending.
        raise ValidationError(
            _(
                "A mission approval is pending: withdraw it before changing the dates, the "
                "destination or the payer."
            ),
            code="order_pending",
        )
    merged_texts = {**{n: str(current_texts[n]) for n in MISSION_TEXTS}, **texts}
    _check_mission(thesis, {**values, **merged_texts})
    level = ConfidentialityLevel(fresh.level)
    _check_payer_level(values["payer"], level)
    changed = sorted(
        [n for n in MISSION_FIELDS if values[n] != getattr(fresh, n)]
        + [n for n, t in texts.items() if t != current_texts[n]]
    )
    if not changed:
        return []
    with vault.sealing(user=user, thesis=thesis, level=level, action=Action.TRAVEL_WRITE) as sealer:
        locked = _locked_mission(fresh)
        if locked.status != fresh.status or locked.level != fresh.level:
            raise ValidationError(_("The mission changed meanwhile: try again."), code="stale")
        for name in MISSION_FIELDS:
            setattr(locked, name, values[name])
        invalidated = None
        if frozen and locked.status == MissionStatus.ORDER_GRANTED:
            invalidated = _granted_order(locked)
            locked.status = MissionStatus.PLANNED.value
        locked.save()
        for name, text in texts.items():
            if text == current_texts[name]:
                continue
            if text:
                sealer.seal(locked, name, text)
            else:
                sealer.erase(locked, name)
        audit(AuditAction.MISSION_UPDATED, actor=user, obj=locked, fields=changed)
        if invalidated is not None:
            audit(
                AuditAction.MISSION_ORDER_INVALIDATED,
                actor=user,
                obj=invalidated,
                thesis=locked.thesis,
                order_id=str(invalidated.pk),
                fields=frozen,
            )
    return changed


def change_mission_level(
    *, mission: Mission, user: User, level: ConfidentialityLevel, recent_mfa: bool
) -> Mission:
    """Move the mission's texts to ``level`` (second factor)."""
    thesis = _fresh(mission.thesis)
    _require(user, Action.TRAVEL_WRITE, thesis, "mission_level")
    _require_mfa(recent_mfa)
    level = _clean_level(level)
    _check_open(thesis)
    fresh = Mission.objects.get(pk=mission.pk)
    _check_payer_level(fresh.payer, level)
    previous = fresh.level
    if level.value == previous:
        return fresh
    with vault.sealing(user=user, thesis=thesis, level=level, action=Action.TRAVEL_WRITE) as sealer:
        locked = _locked_mission(fresh)
        if locked.level != previous or locked.payer != fresh.payer:
            raise ValidationError(_("The mission changed meanwhile: try again."), code="stale")
        for name in MISSION_TEXTS:
            sealer.reseal(locked, name)
        for order in MissionOrder.objects.filter(mission=locked):
            for name in ORDER_TEXTS:
                sealer.reseal(order, name)
        locked.level = level.value
        locked.save()
        audit(
            AuditAction.MISSION_LEVEL_CHANGED,
            actor=user,
            obj=locked,
            **_from_to(previous, level.value),
        )
    # The official order follows, in the vault's own transaction (CLAUDE.md: one
    # transaction per vault service). Meanwhile official_order_restriction keeps the
    # document no more open than the mission.
    document_id = locked.official_order_document_id
    if document_id is not None:
        document = Document.objects.get(pk=document_id)
        if document.confidentiality != level.value:
            vault.change_level(document=document, level=level, user=user)
    return locked


def _set_status(mission: Mission, user: User, target: MissionStatus) -> None:
    previous = mission.status
    mission.status = target.value
    mission.save()
    audit(
        AuditAction.MISSION_STATUS_CHANGED,
        actor=user,
        obj=mission,
        **_from_to(previous, target.value),
    )


def cancel_mission(*, mission: Mission, user: User) -> Mission:
    thesis = _fresh(mission.thesis)
    _require(user, Action.TRAVEL_WRITE, thesis, "mission_cancel")
    _check_open(thesis)
    with transaction.atomic():
        locked = _locked_mission(mission)
        if locked.status not in _OPEN_MISSION:
            raise _terminal()
        pending = (
            MissionOrder.objects.select_for_update()
            .filter(mission=locked, decision=OrderDecision.PENDING)
            .first()
        )
        if pending is not None:
            _withdraw(pending)
        locked.status, previous = MissionStatus.CANCELLED.value, locked.status
        locked.save()
        if pending is not None:
            audit(
                AuditAction.MISSION_ORDER_WITHDRAWN,
                actor=user,
                obj=pending,
                thesis=locked.thesis,
                order_id=str(pending.pk),
            )
        audit(
            AuditAction.MISSION_STATUS_CHANGED,
            actor=user,
            obj=locked,
            **_from_to(previous, MissionStatus.CANCELLED.value),
        )
    return locked


def _granted_order(mission: Mission) -> MissionOrder | None:
    return (
        MissionOrder.objects.filter(mission=mission, decision=OrderDecision.GRANTED)
        .order_by("-decided_at", "-pk")
        .first()
    )


def order_covers(mission: Mission) -> bool:
    """Whether a granted mission order covers the mission as it is now: its latest
    grant, whose snapshot (dates, destination, payer) still matches."""
    if mission.status != MissionStatus.ORDER_GRANTED:
        return False
    order = _granted_order(mission)
    return order is not None and order.snapshot_matches(mission)


def mark_done(*, mission: Mission, user: User) -> Mission:
    """Back from the mission: after its return date (Paris); a mission paid by ST or
    the LIRMM needs a granted mission order that still covers it."""
    thesis = _fresh(mission.thesis)
    _require(user, Action.TRAVEL_WRITE, thesis, "mission_done")
    _check_open(thesis)
    with transaction.atomic():
        locked = _locked_mission(mission)
        if locked.status not in _OPEN_MISSION:
            raise _terminal()
        if locked.payer != Payer.OTHER and not order_covers(locked):
            raise ValidationError(
                _("A granted mission approval is needed first."), code="order_required"
            )
        if locked.end_date > timezone.localdate():
            raise ValidationError(_("The mission is not over yet."), code="not_returned")
        _set_status(locked, user, MissionStatus.DONE)
    return locked


# --------------------------------------------------------------------------- reading


def mission_list(user: Any, thesis: Thesis) -> list[Mission]:
    _require_reader(user, thesis, "mission_list")
    return list(Mission.objects.visible_to(user, thesis).select_related("edition__venue"))


def mission_for(user: Any, thesis: Thesis, mission_id: Any, operation: str) -> Mission:
    """The mission of ``thesis`` if ``user`` may see it. A hidden mission (a
    co-supervisor and a mission elsewhere) is refused exactly like an unknown one."""
    _require_reader(user, thesis, operation)
    try:
        mission = (
            Mission.objects.visible_to(user, thesis)
            .select_related("edition__venue")
            .filter(pk=mission_id)
            .first()
        )
    except (ValueError, ValidationError):
        mission = None
    if mission is None:
        raise _deny(user, operation, "unknown_mission", thesis=thesis)
    return mission


def mission_texts(user: Any, mission: Mission) -> dict[str, Any]:
    """{name: text or vault.RESTRICTED} ("" when none) of a mission ``user`` sees."""
    thesis = _fresh(mission.thesis)
    visible = mission_for(user, thesis, mission.pk, "mission_texts")
    opened = vault.open_texts(
        user, thesis, [(visible, n) for n in MISSION_TEXTS], action=Action.TRAVEL_READ
    )
    return {n: opened.get((visible.pk, n), "") for n in MISSION_TEXTS}


# --------------------------------------------------------------------------- links

# What a mission may link, and the right that reads it.
LINKS: Mapping[str, tuple[type[Any], Action]] = {
    "meeting": (Meeting, Action.MEETING_READ),
    "submission": (Submission, Action.SUBMISSION_READ),
    "edition": (Edition, Action.CONFERENCE_READ),
}


def link_for(user: Any, thesis: Thesis, kind: str, raw: Any) -> Any | None:
    """The object of ``thesis`` named by ``raw`` (a meeting, a submission, an
    edition) if ``user`` may read it; None otherwise. A malformed id, an unknown
    one, one of another thesis and an object ``user`` may not read are refused
    ALIKE: the same None, the same journal entry (reason unknown_link and the kind,
    nothing about which case). An empty ``raw`` is no link: None, no entry."""
    model, action = LINKS[kind]
    if raw in (None, ""):
        return None
    found = None
    try:
        uuid.UUID(str(raw))
    except ValueError:
        pass
    else:
        found = model.objects.filter(pk=str(raw), thesis=thesis).first()
    if found is None or not can(user, action, thesis):
        _deny(user, "mission_link", "unknown_link", thesis=thesis, kind=kind)
        return None
    return found


# =========================================================================== orders

ORDER_TEXTS = ("comment", "estimated_total")
ORDER_SNAPSHOT_FIELDS = ("start_date", "end_date", "destination", "payer")
DECIDER_ROLE = {Payer.ST.value: Role.INDUSTRIAL_TUTOR, Payer.LIRMM.value: Role.DIRECTOR}


def _deciders(thesis: Thesis, payer: str, level: ConfidentialityLevel) -> list[User]:
    """Active members who would decide an order of ``payer`` and read ``level``."""
    role = DECIDER_ROLE[payer]
    users = User.objects.filter(
        pk__in=Membership.objects.active()
        .filter(thesis=thesis, role=role)
        .values_list("user_id", flat=True),
        is_active=True,
    )
    return [
        u
        for u in users
        if can(u, Action.VALIDATE_MISSION_ORDER, thesis) and can_access_level(u, thesis, level)
    ]


def _withdraw(order: MissionOrder) -> None:
    order.decision = OrderDecision.WITHDRAWN.value
    order.decided_at = timezone.now()
    order.save()


def _decided() -> ValidationError:
    return ValidationError(_("This mission approval is already decided."), code="decided")


def request_order(*, mission: Mission, user: User, estimated_total: str) -> MissionOrder:
    """The student asks the payer's person for a mission order; the estimate is
    sealed at the mission's level, the dates, destination and payer are frozen."""
    thesis = _fresh(mission.thesis)
    _require(user, Action.TRAVEL_WRITE, thesis, "order_request")
    _check_open(thesis)
    fresh = Mission.objects.get(pk=mission.pk)
    if fresh.payer not in ORDER_PAYERS:
        raise ValidationError(_("No mission approval for another body."), code="no_order")
    if fresh.status == MissionStatus.ORDER_REQUESTED:
        raise ValidationError(_("A mission approval is already pending."), code="already_pending")
    if fresh.status not in (MissionStatus.PLANNED, MissionStatus.ORDER_REFUSED):
        raise _terminal()
    try:
        cents = amounts.parse(estimated_total)
    except (TypeError, ValueError):
        raise ValidationError(_("An amount in euros, such as 123,45."), code="amount") from None
    level = ConfidentialityLevel(fresh.level)
    if not _deciders(thesis, fresh.payer, level):
        raise ValidationError(
            _("Nobody active can decide for this payer and read the mission."), code="no_decider"
        )
    with vault.sealing(user=user, thesis=thesis, level=level, action=Action.TRAVEL_WRITE) as sealer:
        locked = _locked_mission(fresh)
        if locked.status not in (MissionStatus.PLANNED, MissionStatus.ORDER_REFUSED):
            raise ValidationError(
                _("A mission approval is already pending."), code="already_pending"
            )
        if locked.level != fresh.level or locked.payer != fresh.payer:
            raise ValidationError(_("The mission changed meanwhile: try again."), code="stale")
        order = MissionOrder(
            thesis=locked.thesis,
            mission=locked,
            requested_by=user,
            payer=locked.payer,
            snapshot_start=locked.start_date,
            snapshot_end=locked.end_date,
            snapshot_destination=locked.destination,
            created_by=user,
        )
        order.save(force_insert=True)
        sealer.seal(order, "estimated_total", amounts.canonical(cents))
        locked.status = MissionStatus.ORDER_REQUESTED.value
        locked.save()
        audit(
            AuditAction.MISSION_ORDER_REQUESTED,
            actor=user,
            obj=order,
            thesis=locked.thesis,
            order_id=str(order.pk),
            payer=order.payer,
            start_date=order.snapshot_start.isoformat(),
            end_date=order.snapshot_end.isoformat(),
        )
    return order


def withdraw_order(*, order: MissionOrder, user: User) -> MissionOrder:
    thesis = _fresh(order.thesis)
    _require(user, Action.TRAVEL_WRITE, thesis, "order_withdraw")
    _check_open(thesis)
    with transaction.atomic():
        _lock_thesis(thesis.pk)
        locked = MissionOrder.objects.select_for_update().get(pk=order.pk)
        if locked.decision != OrderDecision.PENDING:
            raise _decided()
        mission = Mission.objects.select_for_update().get(pk=locked.mission_id)
        _withdraw(locked)
        mission.status = MissionStatus.PLANNED.value
        mission.save()
        audit(
            AuditAction.MISSION_ORDER_WITHDRAWN,
            actor=user,
            obj=locked,
            thesis=mission.thesis,
            order_id=str(locked.pk),
        )
    return locked


def check_decider(user: Any, order: MissionOrder) -> MissionOrder:
    """``user`` decides this order: VALIDATE_MISSION_ORDER, the payer's role (tutor
    for ST, director for the LIRMM) and read access to the mission's level. Refused
    (journaled) otherwise. Also called by the view BEFORE the second factor."""
    thesis = _fresh(order.thesis)
    _require(user, Action.VALIDATE_MISSION_ORDER, thesis, "order_decide")
    fresh = MissionOrder.objects.select_related("mission").get(pk=order.pk)
    if DECIDER_ROLE[fresh.payer] not in roles_of(user, thesis):
        raise _deny(user, "order_decide", "wrong_payer", thesis=thesis)
    if not can_access_level(user, thesis, ConfidentialityLevel(fresh.mission.level)):
        raise _deny(user, "order_decide", "unreadable_level", thesis=thesis)
    return fresh


def decide_order(
    *,
    order: MissionOrder,
    user: User,
    decision: str,
    recent_mfa: bool,
    reason: str = "",
    comment: str = "",
) -> MissionOrder:
    """The payer's person (tutor for ST, director for the LIRMM), who reads the
    mission, grants or refuses (a reason from the closed list); second factor asked
    AFTER the rights. A decided order is never decided again."""
    thesis = _fresh(order.thesis)
    fresh = check_decider(user, order)
    level = ConfidentialityLevel(fresh.mission.level)
    _require_mfa(recent_mfa)
    _check_open(thesis)
    if decision not in (OrderDecision.GRANTED, OrderDecision.REFUSED):
        raise ValidationError(_("Grant or refuse."), code="decision")
    if decision == OrderDecision.REFUSED and reason not in OrderRefusalReason.values:
        raise ValidationError(_("Choose a reason for the refusal."), code="reason")
    if decision == OrderDecision.GRANTED and reason:
        raise ValidationError(_("A grant has no reason."), code="reason")
    text = _clean_text(comment, "comment")
    if fresh.decision != OrderDecision.PENDING:
        raise _decided()
    with vault.sealing(
        user=user, thesis=thesis, level=level, action=Action.VALIDATE_MISSION_ORDER
    ) as sealer:
        _lock_thesis(thesis.pk)
        locked = MissionOrder.objects.select_for_update().get(pk=fresh.pk)
        if locked.decision != OrderDecision.PENDING:
            raise _decided()
        mission = Mission.objects.select_for_update().get(pk=locked.mission_id)
        if mission.level != level.value:
            raise ValidationError(_("The mission changed meanwhile: try again."), code="stale")
        locked.decision = decision
        locked.reason = reason if decision == OrderDecision.REFUSED else ""
        locked.decided_by = user
        locked.decided_at = timezone.now()
        locked.save()
        if text:
            sealer.seal(locked, "comment", text)
        granted = decision == OrderDecision.GRANTED
        mission.status = (
            MissionStatus.ORDER_GRANTED if granted else MissionStatus.ORDER_REFUSED
        ).value
        mission.save()
        details: dict[str, Any] = {"order_id": str(locked.pk), "payer": locked.payer}
        if not granted:
            details["reason"] = locked.reason
        audit(
            AuditAction.MISSION_ORDER_GRANTED if granted else AuditAction.MISSION_ORDER_REFUSED,
            actor=user,
            obj=locked,
            thesis=mission.thesis,
            **details,
        )
    return locked


@dataclass(frozen=True)
class OrderView:
    order: MissionOrder
    estimate: Any  # cents, vault.RESTRICTED, or None
    comment: Any  # str, vault.RESTRICTED, or ""


def orders_of(user: Any, mission: Mission) -> list[OrderView]:
    """The orders of a mission ``user`` sees, texts opened as their level allows."""
    thesis = _fresh(mission.thesis)
    visible = mission_for(user, thesis, mission.pk, "order_read")
    orders = list(
        MissionOrder.objects.filter(mission=visible).select_related("decided_by", "requested_by")
    )
    opened = vault.open_texts(
        user, thesis, [(o, n) for o in orders for n in ORDER_TEXTS], action=Action.TRAVEL_READ
    )
    out = []
    for o in orders:
        raw = opened.get((o.pk, "estimated_total"))
        estimate = amounts.from_canonical(raw) if isinstance(raw, str) else raw
        out.append(OrderView(order=o, estimate=estimate, comment=opened.get((o.pk, "comment"), "")))
    return out


def pending_orders_for(user: Any, thesis: Thesis) -> list[MissionOrder]:
    """The pending orders ``user`` decides (tutor: ST; director: LIRMM)."""
    if not can(user, Action.VALIDATE_MISSION_ORDER, thesis):
        return []
    roles = roles_of(user, thesis)
    payers = [payer for payer, role in DECIDER_ROLE.items() if role in roles]
    return list(
        MissionOrder.objects.filter(thesis=thesis, decision=OrderDecision.PENDING, payer__in=payers)
        .select_related("mission")
        .order_by("snapshot_start", "pk")
    )


def order_for(user: Any, thesis: Thesis, order_id: Any, operation: str) -> MissionOrder:
    """An order of ``thesis`` whose mission ``user`` sees; refused like an unknown
    one otherwise."""
    _require_reader(user, thesis, operation)
    try:
        order = (
            MissionOrder.objects.filter(
                pk=order_id,
                thesis=thesis,
                mission__in=Mission.objects.visible_to(user, thesis),
            )
            .select_related("mission")
            .first()
        )
    except (ValueError, ValidationError):
        order = None
    if order is None:
        raise _deny(user, operation, "unknown_order", thesis=thesis)
    return order


# --------------------------------------------------------------------------- official order

OFFICIAL_ORDER_TITLE = "Ordre de mission officiel"  # in clear (M2): fixed, says nothing


def attach_official_order(*, mission: Mission, user: User, upload: Any) -> Document:
    """Store the official mission order (issued outside the platform) in the vault at
    the mission's level, as a new version once there is one, and tick the checklist.
    The vault stores it in its own transaction, then the mission is linked in another
    (CLAUDE.md: never two vault services, nor a vault service and audit(), mixed)."""
    thesis = _fresh(mission.thesis)
    _require(user, Action.TRAVEL_WRITE, thesis, "official_order")
    _check_open(thesis)
    fresh = Mission.objects.get(pk=mission.pk)
    if fresh.status == MissionStatus.CANCELLED:
        raise _terminal()
    level = ConfidentialityLevel(fresh.level)
    if fresh.official_order_document_id is None:
        document = vault.store(
            thesis=thesis, upload=upload, title=OFFICIAL_ORDER_TITLE, level=level, user=user
        )
    else:
        document = Document.objects.get(pk=fresh.official_order_document_id)
        vault.add_version(document=document, upload=upload, user=user)
        document.refresh_from_db()
    with transaction.atomic():
        locked = _locked_mission(fresh)
        if locked.official_order_document_id not in (None, document.pk):
            raise ValidationError(_("The mission changed meanwhile: try again."), code="stale")
        locked.official_order_document = document
        locked.official_order_obtained = True
        locked.save()
        audit(
            AuditAction.MISSION_OFFICIAL_ORDER_ATTACHED,
            actor=user,
            obj=locked,
            document_id=str(document.pk),
            version=document.current_version,
        )
    return document


def official_order_missing(mission: Mission) -> bool:
    """A mission paid by ST or the LIRMM whose official order is not ticked as
    obtained: a warning when it is marked done, never a refusal."""
    return mission.payer in ORDER_PAYERS and not mission.official_order_obtained
