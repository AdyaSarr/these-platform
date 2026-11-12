"""Team management: business rules on memberships. Views stay thin and call these.

Each mutating service re-checks the actor's right with ``can()`` on the TARGET's
thesis (defence in depth: a view that forgot its guard still cannot act), locks
the row (``select_for_update``) and runs in a transaction, so two concurrent
requests cannot both succeed on the same membership.

Database constraints (models.Membership.Meta) remain the last word; their
violations are translated into ``TeamError`` with a message fit for the user.
"""

from __future__ import annotations

import datetime as dt

from django.core.exceptions import PermissionDenied
from django.db import IntegrityError, transaction
from django.utils import timezone
from django.utils.translation import gettext as _
from django.utils.translation import gettext_noop

from .models import Membership, Organization, Thesis, User
from .permissions import Action, can
from .roles import Role

CSI_DEFAULT_PERIOD = dt.timedelta(days=60)
CSI_MAX_EXTENSION_DAYS = 90
CSI_DEFAULT_EXTENSION_DAYS = 60


class TeamError(Exception):
    """A request the team rules refuse. The message is safe to show to the user."""


_CONSTRAINT_MESSAGES = {
    "accounts_membership_no_overlap": gettext_noop(
        "This person already holds this role on the thesis."
    ),
    "accounts_membership_one_phd_student": gettext_noop("This thesis already has a PhD student."),
    "accounts_membership_csi_independent": gettext_noop(
        "A CSI member cannot also be the PhD student or part of the supervision."
    ),
}


def _constraint_name(exc: IntegrityError) -> str | None:
    diag = getattr(exc.__cause__, "diag", None)  # psycopg error details
    name = getattr(diag, "constraint_name", None)
    return name if isinstance(name, str) else None


def _require(by: User, action: Action, thesis: Thesis) -> None:
    if not can(by, action, thesis):
        raise PermissionDenied


def _is_active(m: Membership, at: dt.datetime) -> bool:
    return m.active_from <= at and (m.active_until is None or at < m.active_until)


def _locked(m: Membership) -> Membership:
    return Membership.objects.select_for_update().select_related("thesis").get(pk=m.pk)


def _create(
    *,
    user: User,
    thesis: Thesis,
    role: Role,
    organization: Organization,
    start: dt.datetime,
    created_by: User | None,
) -> Membership:
    until = start + CSI_DEFAULT_PERIOD if role == Role.CSI_MEMBER else None
    try:
        with transaction.atomic():  # savepoint: the caller's transaction survives
            return Membership.objects.create(
                user=user,
                thesis=thesis,
                role=role,
                organization=organization,
                active_from=start,
                active_until=until,
                created_by=created_by,
            )
    except IntegrityError as exc:
        message = _CONSTRAINT_MESSAGES.get(_constraint_name(exc) or "")
        if message is None:
            raise
        raise TeamError(_(message)) from exc


def add_membership(
    *,
    user: User,
    thesis: Thesis,
    role: Role,
    organization: Organization,
    created_by: User | None = None,
) -> Membership:
    """Grant ``role`` from now on. A CSI membership ends after CSI_DEFAULT_PERIOD.

    No permission check: the caller is invitation acceptance (the inviter's right
    was checked when the invitation was created) or a management command.
    """
    return _create(
        user=user,
        thesis=thesis,
        role=Role(role),
        organization=organization,
        start=timezone.now(),
        created_by=created_by,
    )


def end_membership(membership: Membership, *, by: User) -> Membership:
    """Remove a member: the period ends now; the row is kept for the history."""
    with transaction.atomic():
        m = _locked(membership)
        _require(by, Action.REMOVE_MEMBER, m.thesis)
        if m.role == Role.PHD_STUDENT:
            raise TeamError(_("The PhD student cannot be removed from their thesis."))
        now = timezone.now()
        if not _is_active(m, now):
            raise TeamError(_("This membership is not active."))
        m.active_until = now
        m.save(update_fields=["active_until", "updated_at"])
        return m


def change_role(membership: Membership, new_role: Role, *, by: User) -> Membership:
    """Close the current membership and open one with ``new_role`` at the same instant."""
    new_role = Role(new_role)
    with transaction.atomic():
        m = _locked(membership)
        _require(by, Action.CHANGE_ROLE, m.thesis)
        if Role.PHD_STUDENT in (m.role, new_role):
            raise TeamError(_("The PhD student role cannot be given or taken away."))
        if new_role == m.role:
            raise TeamError(_("This person already holds this role."))
        now = timezone.now()
        if not _is_active(m, now):
            raise TeamError(_("This membership is not active."))
        m.active_until = now
        m.save(update_fields=["active_until", "updated_at"])
        # If the new row violates a constraint, TeamError leaves this
        # transaction and the closing above is rolled back.
        return _create(
            user=m.user,
            thesis=m.thesis,
            role=new_role,
            organization=m.organization,
            start=now,
            created_by=by,
        )


def extend_membership(
    membership: Membership, *, by: User, days: int = CSI_DEFAULT_EXTENSION_DAYS
) -> Membership:
    """Push back the end of an active CSI membership by ``days`` (1 to 90, default 60)."""
    if not isinstance(days, int) or not 1 <= days <= CSI_MAX_EXTENSION_DAYS:
        raise TeamError(
            _("The extension must be between 1 and %(max)d days.") % {"max": CSI_MAX_EXTENSION_DAYS}
        )
    with transaction.atomic():
        m = _locked(membership)
        _require(by, Action.EXTEND_MEMBERSHIP, m.thesis)
        if m.role != Role.CSI_MEMBER or m.active_until is None:
            raise TeamError(_("Only a CSI membership can be extended."))
        if not _is_active(m, timezone.now()):
            raise TeamError(_("An expired CSI membership cannot be extended; invite again."))
        m.active_until += dt.timedelta(days=days)
        m.save(update_fields=["active_until", "updated_at"])
        return m
