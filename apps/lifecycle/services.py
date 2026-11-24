"""End of the thesis (M13a, docs/modules/M13-fin-de-these.md, « Passages »).

- ACTIVE -> DEFENDED: a director (DECLARE_DEFENSE), recent second factor, a past
  date; reversible (DEFENDED -> ACTIVE) with a reason of a closed list.
- -> CLOSED: two persons. A director or the PhD student requests (REQUEST_CLOSURE,
  recent second factor); the OTHER party confirms within 14 days (recent second
  factor). From DEFENDED after a minimal delay (reason ``defended``), or from
  ACTIVE on abandonment (a reason of a closed list). CLOSED is irreversible.
- After CLOSED: the co-supervisors, CSI members, guests and the tutor leave at
  once, the director when ``access_until`` comes (daily run), the student stays.

Each passage locks the thesis row; audit() is the last call of its transaction;
the ends of membership and the revocation of invitations follow, each in its own
transaction. The journal carries codes only.
"""

from __future__ import annotations

import calendar
import datetime as dt
import functools
from typing import Any

from django.conf import settings
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.utils import timezone
from django.utils.formats import date_format
from django.utils.translation import gettext as _

from apps.accounts.models import Membership, Thesis
from apps.accounts.permissions import Action, can, roles_of
from apps.accounts.roles import Role
from apps.vault.errors import ReauthenticationRequiredError

from .models import (
    ABANDON_REASONS,
    CancelReason,
    ClosureReason,
    ClosureRequest,
    LifecycleNotification,
)

S = Thesis.Status
REQUEST_LIFETIME = dt.timedelta(days=14)
N = LifecycleNotification.Kind


def _min_months() -> int:
    """Delay between the defence and the closure: « à confirmer (Q4) »."""
    return int(getattr(settings, "THESIS_CLOSE_MIN_MONTHS", 3))


def _director_access_months() -> int:
    return int(getattr(settings, "THESIS_DIRECTOR_ACCESS_MONTHS", 3))


def _months_later(day: dt.date, months: int) -> dt.date:
    index = day.year * 12 + (day.month - 1) + months
    year, month = divmod(index, 12)
    last = calendar.monthrange(year, month + 1)[1]
    return day.replace(year=year, month=month + 1, day=min(day.day, last))


def _closable_from(thesis: Thesis) -> dt.date | None:
    return _months_later(thesis.defended_on, _min_months()) if thesis.defended_on else None


# --------------------------------------------------------------------------- checks


def _require(user: Any, action: Action, thesis: Thesis) -> None:
    if not can(user, action, thesis):
        raise PermissionDenied


def _reauth(recent_mfa: bool) -> None:
    if not recent_mfa:
        raise ReauthenticationRequiredError


def _invalid(message: str, code: str) -> ValidationError:
    return ValidationError(message, code=code)


def _locked(thesis: Thesis) -> Thesis:
    return Thesis.objects.select_for_update().get(pk=thesis.pk)


def _audit(action: str, user: Any, thesis: Thesis, **details: Any) -> None:
    from apps.vault.audit import AuditAction, audit

    audit(AuditAction(action), actor=user, thesis=thesis, **details)


def _record(kind: str, obj: Any, object_type: str, thesis: Thesis, user: Any) -> None:
    from apps.activity import events

    events.record(kind=kind, obj=obj, object_type=object_type, thesis=thesis, actor=user)


def _members(thesis: Thesis, role: Role) -> list[Any]:
    rows = Membership.objects.active().filter(thesis=thesis, role=role).select_related("user")
    return [m.user for m in rows]


def _queue(thesis: Thesis, kind: str, users: list[Any], request: Any = None) -> None:
    """Rows in the caller's transaction; sent after commit, by their id only."""
    from .tasks import send_lifecycle_notification

    for user in users:
        row = LifecycleNotification.objects.create(
            thesis=thesis, recipient=user, kind=kind, closure_request=request
        )
        transaction.on_commit(functools.partial(send_lifecycle_notification.delay, str(row.pk)))


def _open_requests(thesis: Thesis) -> Any:
    return ClosureRequest.objects.filter(
        thesis=thesis, confirmed_at__isnull=True, withdrawn_at__isnull=True,
        expired_at__isnull=True,
    )  # fmt: skip


def _withdraw_open(thesis: Thesis) -> None:
    """A declaration or a cancellation of the defence makes an open request moot."""
    _open_requests(thesis).update(withdrawn_at=timezone.now())


# --------------------------------------------------------------------------- defence


def declare_defense(*, thesis: Thesis, user: Any, defended_on: dt.date, recent_mfa: bool) -> Thesis:
    _require(user, Action.DECLARE_DEFENSE, thesis)
    _reauth(recent_mfa)
    if not isinstance(defended_on, dt.date) or isinstance(defended_on, dt.datetime):
        raise _invalid(_("Give the date of the defence."), "date")
    with transaction.atomic():
        locked = _locked(thesis)
        if locked.status != S.ACTIVE:
            raise _invalid(_("Only a thesis in progress can be declared defended."), "status")
        if not locked.first_enrollment_date <= defended_on <= timezone.localdate():
            raise _invalid(
                _("The defence date must be past and after the first enrollment (%(date)s).")
                % {"date": date_format(locked.first_enrollment_date, "j F Y")},
                "date",
            )
        locked.status = S.DEFENDED
        locked.defended_on = defended_on
        locked.save(update_fields=["status", "defended_on", "updated_at"])
        _withdraw_open(locked)
        _record("THESIS_DEFENDED", locked, "THESIS", locked, user)
        _queue(locked, N.DEFENDED, _members(locked, Role.PHD_STUDENT))
        _audit("THESIS_DEFENDED", user, locked, defended_on=defended_on.isoformat())
    _after_passage(locked, "thesis_defended")
    return locked


def cancel_defense(*, thesis: Thesis, user: Any, reason: str, recent_mfa: bool) -> Thesis:
    _require(user, Action.DECLARE_DEFENSE, thesis)
    _reauth(recent_mfa)
    if reason not in CancelReason.values:
        raise _invalid(_("Choose a reason in the list."), "reason")
    with transaction.atomic():
        locked = _locked(thesis)
        if locked.status != S.DEFENDED:
            raise _invalid(_("The thesis is not declared defended."), "status")
        locked.status = S.ACTIVE
        locked.defended_on = None
        locked.save(update_fields=["status", "defended_on", "updated_at"])
        _withdraw_open(locked)
        _record("THESIS_DEFENSE_CANCELLED", locked, "THESIS", locked, user)
        _queue(locked, N.DEFENSE_CANCELLED, _members(locked, Role.PHD_STUDENT))
        _audit("THESIS_DEFENSE_CANCELLED", user, locked, reason=reason)
    return locked


# --------------------------------------------------------------------------- closure


def _side(user: Any, thesis: Thesis) -> Role:
    return Role.PHD_STUDENT if Role.PHD_STUDENT in roles_of(user, thesis) else Role.DIRECTOR


def _check_reason(thesis: Thesis, reason: str) -> None:
    """The reason fits the status; a defended thesis waits for the minimal delay."""
    if thesis.status == S.ACTIVE:
        if reason not in ABANDON_REASONS:
            raise _invalid(_("A thesis in progress is closed on abandonment only."), "reason")
    elif thesis.status == S.DEFENDED:
        if reason != ClosureReason.DEFENDED:
            raise _invalid(_("A defended thesis is closed as defended."), "reason")
        start = _closable_from(thesis)
        if start is None or timezone.localdate() < start:
            raise _invalid(_("Too early: the closure comes after the minimal delay."), "too_early")
    else:
        raise _invalid(_("This thesis cannot be closed."), "status")


def _expire_stale(thesis: Thesis | None = None) -> int:
    """Open requests past their expiry: marked expired, journaled once each."""
    stale = ClosureRequest.objects.filter(
        confirmed_at__isnull=True, withdrawn_at__isnull=True, expired_at__isnull=True,
        expires_at__lte=timezone.now(),
    )  # fmt: skip
    if thesis is not None:
        stale = stale.filter(thesis=thesis)
    count = 0
    for request_id in list(stale.values_list("pk", flat=True)):
        with transaction.atomic():
            request = ClosureRequest.objects.select_for_update().get(pk=request_id)
            if request.expired_at or request.confirmed_at or request.withdrawn_at:
                continue
            request.expired_at = timezone.now()
            request.save(update_fields=["expired_at", "updated_at"])
            _audit("THESIS_CLOSURE_EXPIRED", None, request.thesis)
            count += 1
    return count


def request_closure(*, thesis: Thesis, user: Any, reason: str, recent_mfa: bool) -> ClosureRequest:
    _require(user, Action.REQUEST_CLOSURE, thesis)
    _reauth(recent_mfa)
    if reason not in ClosureReason.values:
        raise _invalid(_("Choose a reason in the list."), "reason")
    side = _side(user, thesis)
    _expire_stale(thesis)
    with transaction.atomic():
        locked = _locked(thesis)
        _check_reason(locked, reason)
        if _open_requests(locked).exists():
            raise _invalid(_("A closure request is already open."), "open_request")
        request = ClosureRequest.objects.create(
            thesis=locked, requested_by=user, requester_role=side.value, reason=reason,
            expires_at=timezone.now() + REQUEST_LIFETIME, created_by=user,
        )  # fmt: skip
        other = Role.DIRECTOR if side is Role.PHD_STUDENT else Role.PHD_STUDENT
        _record("THESIS_CLOSURE_REQUESTED", request, "CLOSURE_REQUEST", locked, user)
        _queue(locked, N.CLOSURE_REQUESTED, _members(locked, other), request)
        _audit("THESIS_CLOSURE_REQUESTED", user, locked, reason=reason)
    return request


def confirm_closure(*, request: ClosureRequest, user: Any, recent_mfa: bool) -> Thesis:
    thesis = Thesis.objects.get(pk=request.thesis_id)
    _require(user, Action.REQUEST_CLOSURE, thesis)
    _reauth(recent_mfa)
    with transaction.atomic():
        locked = _locked(thesis)
        fresh = ClosureRequest.objects.select_for_update().get(pk=request.pk)
        if not fresh.is_open:
            raise _invalid(_("This request is no longer open."), "not_open")
        wanted = Role.DIRECTOR if fresh.requester_role == Role.PHD_STUDENT else Role.PHD_STUDENT
        if user.pk == fresh.requested_by_id or wanted not in roles_of(user, locked):
            raise PermissionDenied  # the other party confirms, never the requester's side
        _check_reason(locked, fresh.reason)
        now = timezone.now()
        locked.status = S.CLOSED
        locked.closed_at = now
        locked.access_until = _months_later(timezone.localdate(now), _director_access_months())
        locked.save(update_fields=["status", "closed_at", "access_until", "updated_at"])
        fresh.confirmed_by = user
        fresh.confirmed_at = now
        fresh.save(update_fields=["confirmed_by", "confirmed_at", "updated_at"])
        _record("THESIS_CLOSED", locked, "THESIS", locked, user)
        _audit("THESIS_CLOSED", user, locked, reason=fresh.reason)
    _after_passage(locked, "thesis_closed")
    _end_memberships(locked)
    return locked


def withdraw_closure(*, request: ClosureRequest, user: Any) -> ClosureRequest:
    """Either party withdraws an open request; no second factor (nothing opens)."""
    thesis = Thesis.objects.get(pk=request.thesis_id)
    _require(user, Action.REQUEST_CLOSURE, thesis)
    with transaction.atomic():
        fresh = ClosureRequest.objects.select_for_update().get(pk=request.pk)
        if not fresh.is_open:
            raise _invalid(_("This request is no longer open."), "not_open")
        fresh.withdrawn_at = timezone.now()
        fresh.save(update_fields=["withdrawn_at", "updated_at"])
        by = "requester" if user.pk == fresh.requested_by_id else "counterpart"
        _audit("THESIS_CLOSURE_WITHDRAWN", user, thesis, by=by)
    return fresh


# --------------------------------------------------------------------------- after


def _after_passage(thesis: Thesis, reason: str) -> None:
    from apps.accounts.services import revoke_open_invitations

    revoke_open_invitations(thesis, reason=reason)


def _end_memberships(thesis: Thesis) -> int:
    """End what must end on a CLOSED thesis: everyone but the student at once, the
    director when ``access_until`` has come. Each end in its own transaction."""
    from apps.accounts.services import TeamError, end_membership_for_closure

    fresh = Thesis.objects.get(pk=thesis.pk)
    if fresh.status != S.CLOSED:
        return 0
    director_due = fresh.access_until is not None and timezone.localdate() >= fresh.access_until
    count = 0
    rows = Membership.objects.active().filter(thesis=fresh).exclude(role=Role.PHD_STUDENT)
    for membership in rows.order_by("active_from", "pk"):
        if membership.role == Role.DIRECTOR:
            if not director_due:
                continue
            reason = "access_period_ended"
        else:
            reason = "thesis_closed"
        try:
            end_membership_for_closure(membership, reason=reason)
        except TeamError:  # ended meanwhile
            continue
        count += 1
    return count


def daily(today: dt.date | None = None) -> dict[str, int]:
    """Expire the requests past 14 days; end the memberships due on CLOSED theses
    (catching up any end missed at the closure)."""
    expired = _expire_stale()
    ended = sum(_end_memberships(t) for t in Thesis.objects.filter(status=S.CLOSED))
    return {"expired": expired, "ended": ended}


# --------------------------------------------------------------------------- page


def page(user: Any, thesis: Thesis) -> dict[str, Any]:
    """What the « Fin de thèse » page shows, and which forms (writes nothing)."""
    fresh = Thesis.objects.get(pk=thesis.pk)
    request = (
        _open_requests(fresh).filter(expires_at__gt=timezone.now()).select_related("requested_by")
    ).first()
    declares = can(user, Action.DECLARE_DEFENSE, fresh)
    closes = can(user, Action.REQUEST_CLOSURE, fresh)
    held = roles_of(user, fresh)
    start = _closable_from(fresh)
    confirmer = False
    if request is not None and user.pk != request.requested_by_id:
        wanted = Role.DIRECTOR if request.requester_role == Role.PHD_STUDENT else Role.PHD_STUDENT
        confirmer = wanted in held
    can_request = (
        closes
        and request is None
        and (
            fresh.status == S.ACTIVE
            or (fresh.status == S.DEFENDED and start is not None and timezone.localdate() >= start)
        )
    )
    possible = {
        "can_declare": declares and fresh.status == S.ACTIVE,
        "can_cancel": declares and fresh.status == S.DEFENDED,
        "can_request": can_request,
        "can_confirm": closes and confirmer,
        "can_withdraw": closes and request is not None,
    }
    return {
        **possible,
        "why": None if any(possible.values()) else _why(fresh, held, request, start),
        "thesis": fresh,
        "request": request,
        "closable_from": start,
        "abandon": fresh.status == S.ACTIVE,
        "cancel_reasons": CancelReason.choices,
        "abandon_reasons": [(r.value, r.label) for r in ClosureReason if r in ABANDON_REASONS],
        "today": timezone.localdate(),
        # M13b: the way to the export (the student) and to its ST side (the tutor).
        "can_export": can(user, Action.THESIS_EXPORT, fresh),
        "can_st_export": can(user, Action.SET_ST_CONFIDENTIALITY, fresh),
    }


def _why(thesis: Thesis, held: frozenset[Role], request: Any, start: dt.date | None) -> str:
    """Why no step is offered (a code; the page says it): nothing the viewer does
    not read already on the page (its status and dates)."""
    if thesis.status == S.CLOSED:
        return "closed"
    if thesis.status == S.SETUP:
        return "setup"
    if not held & {Role.DIRECTOR, Role.PHD_STUDENT}:
        return "roles"
    if thesis.status == S.DEFENDED and request is None and start is not None:
        return "too_early"
    return "waiting"
