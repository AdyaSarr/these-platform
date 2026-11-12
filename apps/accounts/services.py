"""Team and invitation rules. Views stay thin and call these.

Each mutating service re-checks the actor's right with ``can()`` on the TARGET's
thesis (defence in depth: a view that forgot its guard still cannot act), locks
the row (``select_for_update``) and runs in a transaction, so two concurrent
requests cannot both succeed on the same membership or invitation.

Database constraints (models.Membership.Meta) remain the last word; their
violations are translated into ``TeamError`` with a message fit for the user.
Every change is written to the audit log (``vault.services.audit``).
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import TYPE_CHECKING

from django.core.exceptions import PermissionDenied
from django.db import IntegrityError, transaction
from django.utils import timezone
from django.utils.translation import gettext as _
from django.utils.translation import gettext_noop

from apps.vault import crypto
from apps.vault.services import AuditAction, audit

from . import tasks
from .managers import UserManager
from .models import Invitation, Membership, Organization, Thesis, User
from .permissions import Action, can, roles_of
from .roles import CSI_INCOMPATIBLE_ROLES, Role

if TYPE_CHECKING:
    from django.http import HttpRequest

CSI_DEFAULT_PERIOD = dt.timedelta(days=60)
CSI_MAX_EXTENSION_DAYS = 90
CSI_DEFAULT_EXTENSION_DAYS = 60
MAX_TOKEN_LENGTH = 128


class TeamError(Exception):
    """A request the team rules refuse. The message is safe to show to the user."""


class InvalidInvitationError(Exception):
    """Unknown, expired, used, revoked or unsent invitation. Deliberately uninformative."""


_MSG_SAME_ROLE = gettext_noop("This person already holds this role on the thesis.")
_MSG_CSI = gettext_noop("A CSI member cannot also be the PhD student or part of the supervision.")

_CONSTRAINT_MESSAGES = {
    "accounts_membership_no_overlap": _MSG_SAME_ROLE,
    "accounts_membership_one_phd_student": gettext_noop("This thesis already has a PhD student."),
    "accounts_membership_csi_independent": _MSG_CSI,
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


# --------------------------------------------------------------------------- memberships


def add_membership(
    *,
    user: User,
    thesis: Thesis,
    role: Role,
    organization: Organization,
    created_by: User | None = None,
    request: HttpRequest | None = None,
) -> Membership:
    """Grant ``role`` from now on. A CSI membership ends after CSI_DEFAULT_PERIOD.

    No permission check: the caller is invitation acceptance (the inviter's right
    was checked when the invitation was created) or a management command.
    """
    role = Role(role)
    with transaction.atomic():
        m = _create(
            user=user,
            thesis=thesis,
            role=role,
            organization=organization,
            start=timezone.now(),
            created_by=created_by,
        )
        audit(
            AuditAction.MEMBERSHIP_GRANTED,
            actor=created_by,
            obj=m,
            request=request,
            role=role.value,
        )
        return m


def end_membership(
    membership: Membership, *, by: User, request: HttpRequest | None = None
) -> Membership:
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
        audit(AuditAction.MEMBER_REMOVED, actor=by, obj=m, request=request, role=m.role)
        return m


def change_role(
    membership: Membership, new_role: Role, *, by: User, request: HttpRequest | None = None
) -> Membership:
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
        new = _create(
            user=m.user,
            thesis=m.thesis,
            role=new_role,
            organization=m.organization,
            start=now,
            created_by=by,
        )
        audit(
            AuditAction.MEMBER_ROLE_CHANGED,
            actor=by,
            obj=new,
            request=request,
            old_role=m.role,
            new_role=new_role.value,
            previous_membership=str(m.pk),
        )
        return new


def extend_membership(
    membership: Membership,
    *,
    by: User,
    days: int = CSI_DEFAULT_EXTENSION_DAYS,
    request: HttpRequest | None = None,
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
        audit(
            AuditAction.MEMBERSHIP_EXTENDED,
            actor=by,
            obj=m,
            request=request,
            days=days,
            until=m.active_until.isoformat(),
        )
        return m


# --------------------------------------------------------------------------- invitations


def _check_invitee(email: str, thesis: Thesis, role: Role) -> None:
    """Refuse early what acceptance would refuse anyway (clearer for the inviter)."""
    existing = User.objects.filter(email=email).first()
    if existing is None:
        return
    held = roles_of(existing, thesis)
    if role in held:
        raise TeamError(_(_MSG_SAME_ROLE))
    if (role == Role.CSI_MEMBER and held & CSI_INCOMPATIBLE_ROLES) or (
        role in CSI_INCOMPATIBLE_ROLES and Role.CSI_MEMBER in held
    ):
        raise TeamError(_(_MSG_CSI))


def _create_invitation(
    *,
    thesis: Thesis,
    email: str,
    role: Role,
    organization: Organization,
    by: User | None,
    request: HttpRequest | None,
    enqueue: bool,
) -> Invitation:
    email = UserManager.normalize_email(email)
    with transaction.atomic():
        if by is not None:
            _require(by, Action.INVITE_MEMBER, thesis)
        _check_invitee(email, thesis, role)
        now = timezone.now()
        # One open invitation per (thesis, e-mail, role): re-inviting revokes the old one.
        previous = Invitation.objects.select_for_update().filter(
            thesis=thesis, email=email, role=role, used_at__isnull=True, revoked_at__isnull=True
        )
        for old in previous:
            old.revoked_at = now
            old.save(update_fields=["revoked_at", "updated_at"])
            audit(
                AuditAction.INVITATION_REVOKED,
                actor=by,
                obj=old,
                request=request,
                reason="reissued",
            )
        inv = Invitation.objects.create(
            thesis=thesis,
            email=email,
            role=role,
            organization=organization,
            invited_by=by,
            created_by=by,
        )
        audit(AuditAction.INVITATION_CREATED, actor=by, obj=inv, request=request, role=role.value)
        if enqueue:
            # After commit: the worker must see the row. Only the id goes through
            # the broker; the token is generated by the task itself.
            invitation_id = str(inv.pk)
            transaction.on_commit(lambda: tasks.send_invitation_email.delay(invitation_id))
        return inv


def create_invitation(
    *,
    thesis: Thesis,
    email: str,
    role: Role,
    organization: Organization,
    by: User,
    request: HttpRequest | None = None,
) -> Invitation:
    """Invite ``email`` to hold ``role`` (PhD student only). The e-mail is sent by Celery."""
    role = Role(role)
    if role == Role.PHD_STUDENT:
        raise TeamError(_("The PhD student role cannot be given or taken away."))
    return _create_invitation(
        thesis=thesis,
        email=email,
        role=role,
        organization=organization,
        by=by,
        request=request,
        enqueue=True,
    )


def invite_phd_student(*, thesis: Thesis, email: str, organization: Organization) -> Invitation:
    """System invitation of the PhD student (manage.py create_thesis). Not enqueued."""
    return _create_invitation(
        thesis=thesis,
        email=email,
        role=Role.PHD_STUDENT,
        organization=organization,
        by=None,
        request=None,
        enqueue=False,
    )


def revoke_invitation(
    invitation: Invitation, *, by: User, request: HttpRequest | None = None
) -> Invitation:
    with transaction.atomic():
        inv = Invitation.objects.select_for_update().select_related("thesis").get(pk=invitation.pk)
        _require(by, Action.INVITE_MEMBER, inv.thesis)
        if inv.used_at is not None or inv.revoked_at is not None:
            raise TeamError(_("This invitation is no longer open."))
        inv.revoked_at = timezone.now()
        inv.save(update_fields=["revoked_at", "updated_at"])
        audit(AuditAction.INVITATION_REVOKED, actor=by, obj=inv, request=request)
        return inv


def ensure_usable(inv: Invitation | None) -> Invitation:
    """Raise InvalidInvitationError unless ``inv`` can be accepted right now."""
    if (
        inv is None
        or inv.used_at is not None
        or inv.revoked_at is not None
        or inv.token_hash is None
        or inv.email_status != Invitation.EmailStatus.SENT
        or inv.expires_at is None
        or timezone.now() >= inv.expires_at
    ):
        raise InvalidInvitationError
    return inv


def check_token(token: str) -> Invitation:
    """The open invitation matching ``token``, or InvalidInvitationError.

    The lookup is by digest (an index); the constant-time comparison then guards
    against any timing difference. Timing of the lookup itself reveals nothing
    useful: it depends on SHA-256 of a 256-bit random value.
    """
    if not isinstance(token, str) or not token or len(token) > MAX_TOKEN_LENGTH:
        raise InvalidInvitationError
    inv = Invitation.objects.filter(token_hash=crypto.hash_token(token)).first()
    if inv is None or inv.token_hash is None or not crypto.tokens_match(token, inv.token_hash):
        raise InvalidInvitationError
    return ensure_usable(inv)


def _accept(inv: Invitation, user: User, request: HttpRequest | None) -> Membership:
    m = add_membership(
        user=user,
        thesis=inv.thesis,
        role=Role(inv.role),
        organization=inv.organization,
        created_by=inv.invited_by,
        request=request,
    )
    inv.used_at = timezone.now()
    inv.accepted_by = user
    inv.save(update_fields=["used_at", "accepted_by", "updated_at"])
    audit(AuditAction.INVITATION_ACCEPTED, actor=user, obj=inv, request=request)
    return m


def _lock_usable(invitation_id: uuid.UUID | str) -> Invitation:
    inv = (
        Invitation.objects.select_for_update()
        .select_related("thesis", "organization")
        .filter(pk=invitation_id)
        .first()
    )
    return ensure_usable(inv)


def accept_invitation(
    invitation_id: uuid.UUID | str, *, user: User, request: HttpRequest | None = None
) -> Membership:
    """Existing account: ``user`` must be active and own the invited address."""
    with transaction.atomic():
        inv = _lock_usable(invitation_id)
        if not user.is_active or user.email != inv.email:
            raise InvalidInvitationError
        return _accept(inv, user, request)


def accept_invitation_new_user(
    invitation_id: uuid.UUID | str,
    *,
    first_name: str,
    last_name: str,
    password: str,
    request: HttpRequest | None = None,
) -> User:
    """New account for the invited address. ``password`` was validated by the form."""
    with transaction.atomic():
        inv = _lock_usable(invitation_id)
        if User.objects.filter(email=inv.email).exists():
            raise InvalidInvitationError
        user = User.objects.create_user(
            email=inv.email, password=password, first_name=first_name, last_name=last_name
        )
        _accept(inv, user, request)
        return user
