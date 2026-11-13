"""Business logic of the administrative file (M3).

Every service decides rights HERE, even when a view checked too:
- visibility: the read right of the item's section (SECTION_READ_ACTION);
- submit, withdraw: ADMIN_FILE_WRITE (the student);
- approve, reject: a role of the item's ``validator_roles`` (per-item rule, NOT
  the section's read right), never the cycle's submitter, one approval per
  role and per person in a cycle (the latter enforced by the database);
- reopen: the student or a validator role of the item.
A refusal is audited (ADMIN_ITEM_ACCESS_DENIED) in its own transaction, after
the work transaction rolled back, then PermissionDenied is raised. A state
conflict raises InvalidTransitionError and is not audited (not a security event).

Lock order (CLAUDE.md): thesis row, then item row, then audit() LAST. Locking
the thesis first serializes the validations of one thesis: of two racing last
validations, exactly one sees every mandatory item VALIDATED and activates.
A vault service (store, add_version) runs BEFORE, in its own transaction.
Re-authentication is a view concern (it needs the request).
"""

from __future__ import annotations

import unicodedata
from typing import Any

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, transaction
from django.db.models import QuerySet
from django.utils import timezone
from django.utils.translation import gettext as _

from apps.accounts.models import Thesis, User
from apps.accounts.permissions import (
    LEVEL_ROLES,
    Action,
    allowed_actions,
    can,
    can_access_document,
    roles_of,
)
from apps.accounts.roles import Role
from apps.core.confidentiality import ConfidentialityLevel
from apps.vault import services as vault
from apps.vault.models import Document
from apps.vault.services import AuditAction, audit

from . import catalog
from .models import SECTION_READ_ACTION, ItemApproval, ItemStatus, Reason, RequiredItem, Section
from .transitions import (
    Event,
    FrozenFileError,
    InvalidTransitionError,
    StaleSubmissionError,
    target,
)

__all__ = [
    "allowed_levels",
    "approve",
    "item_for",
    "label_of",
    "reject",
    "reopen",
    "seed_items",
    "submit",
    "visible_items",
    "withdraw",
]

OPEN_THESIS = (Thesis.Status.SETUP, Thesis.Status.ACTIVE)
COMMENT_MAX = 280


class _DeniedError(Exception):
    """Raised inside the work transaction; audited once it has rolled back."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


# --------------------------------------------------------------------------- seeding


def seed_items(thesis: Thesis, *, actor: User | None = None) -> list[str]:
    """Create the catalogue items missing from ``thesis``; return their codes.

    Idempotent: an existing item (same code) is never modified, so the
    student's changes (due date...) survive a new run. Audited only when
    something was created, with the codes only (labels never reach the log).
    """
    created: list[str] = []
    with transaction.atomic():
        for entry in catalog.CATALOG:
            _item, was_created = RequiredItem.objects.get_or_create(
                thesis=thesis,
                code=entry.code,
                defaults={
                    "section": entry.section,
                    "validator_roles": list(entry.validator_roles),
                    "default_level": entry.default_level,
                    "mandatory": entry.mandatory,
                    "attachment_required": entry.attachment_required,
                    "due_date": entry.due_date(thesis),
                    "created_by": actor,
                },
            )
            if was_created:
                created.append(entry.code)
        if created:
            audit(AuditAction.ADMIN_FILE_SEEDED, actor=actor, thesis=thesis, items=created)
    return created


# --------------------------------------------------------------------------- reading


def label_of(item: RequiredItem) -> str:
    """Catalogue label (translated) or the custom item's clear-text label."""
    entry = catalog.by_code(item.code)
    return str(entry.label) if entry is not None else item.label


def visible_items(user: Any, thesis: Thesis) -> QuerySet[RequiredItem]:
    """The items of ``thesis`` whose section ``user`` may read (one query for rights)."""
    actions = allowed_actions(user, thesis)
    sections = [s for s in Section if SECTION_READ_ACTION[s] in actions]
    return RequiredItem.objects.filter(thesis=thesis, section__in=sections)


def _sees(user: Any, item: RequiredItem) -> bool:
    return can(user, SECTION_READ_ACTION[Section(item.section)], item)


def item_for(user: Any, thesis: Thesis, item_id: Any, *, operation: str) -> RequiredItem:
    """The item ``item_id`` of ``thesis`` if ``user`` sees it; else an audited 403.

    Unknown, of another thesis than the URL's, or invisible: the same answer
    (IDOR: the id in the URL never crosses theses).
    """
    try:
        item = RequiredItem.objects.filter(pk=item_id, thesis=thesis).first()
    except (ValueError, ValidationError):
        item = None
    if item is None or not _sees(user, item):
        details: dict[str, Any] = {"obj": item} if item is not None else {"thesis": thesis}
        _audit_denial(user, operation, "not_visible", **details)
        raise PermissionDenied
    return item


def allowed_levels(item: RequiredItem) -> list[ConfidentialityLevel]:
    """Levels at which every validator of ``item`` can read the attachment."""
    validators = {Role(r) for r in item.validator_roles}
    return [level for level in ConfidentialityLevel if validators <= LEVEL_ROLES[level]]


# --------------------------------------------------------------------------- helpers


def _audit_denial(user: Any, operation: str, reason: str, **kwargs: Any) -> None:
    with transaction.atomic():
        audit(
            AuditAction.ADMIN_ITEM_ACCESS_DENIED,
            actor=user,
            operation=operation,
            reason=reason,
            **kwargs,
        )


def _locked(item: RequiredItem) -> RequiredItem:
    """Lock the thesis row, THEN the item row (always in this order)."""
    thesis = Thesis.objects.select_for_update().get(pk=item.thesis_id)
    locked = RequiredItem.objects.select_for_update().get(pk=item.pk)
    locked.thesis = thesis
    if thesis.status not in OPEN_THESIS:
        raise FrozenFileError(_("The thesis is finished: its administrative file is frozen."))
    return locked


def _run(item: RequiredItem, user: Any, operation: str, work: Any) -> RequiredItem:
    """Run ``work(locked_item)`` in one transaction; audit a refusal after rollback."""
    try:
        with transaction.atomic():
            result: RequiredItem = work(_locked(item))
            return result
    except _DeniedError as denied:
        _audit_denial(user, operation, denied.reason, obj=item)
        raise PermissionDenied from None


def _require_sees(user: Any, item: RequiredItem) -> None:
    if not _sees(user, item):
        raise _DeniedError("not_visible")


def _require_writer(user: Any, item: RequiredItem) -> None:
    _require_sees(user, item)
    if not can(user, Action.ADMIN_FILE_WRITE, item):
        raise _DeniedError("not_writer")


def _validator_roles_of(user: Any, item: RequiredItem) -> list[Role]:
    """The user's roles that may validate ``item``, in the item's order."""
    held = roles_of(user, item.thesis)
    return [Role(r) for r in item.validator_roles if Role(r) in held]


def _require_decider(user: Any, item: RequiredItem) -> list[Role]:
    """Approve or reject: a validator role of the item, not the cycle's submitter."""
    _require_sees(user, item)
    roles = _validator_roles_of(user, item)
    if not roles:
        raise _DeniedError("not_validator")
    if item.submitted_by_id is not None and item.submitted_by_id == user.pk:
        raise _DeniedError("submitter")
    return roles


def _clean_reason(reason: Any, comment: str) -> tuple[str, str]:
    try:
        code = Reason(reason).value
    except ValueError:
        raise ValidationError(_("Choose a reason."), code="reason") from None
    comment = (comment or "").strip()
    if len(comment) > COMMENT_MAX:
        raise ValidationError(_("The comment is too long."), code="comment")
    if any(unicodedata.category(c) == "Cc" for c in comment):
        raise ValidationError(_("The comment contains a control character."), code="comment")
    return code, comment


def _transition_details(item: RequiredItem, before: str, **extra: Any) -> dict[str, Any]:
    return {"from": before, "to": item.status, "cycle": item.cycle, **extra}


# --------------------------------------------------------------------------- submit


def _document_title(item: RequiredItem) -> str:
    return f"{_('Administrative file')} - {label_of(item)}"


def submit(
    *,
    item: RequiredItem,
    user: User,
    upload: Any = None,
    document: Document | None = None,
    level: ConfidentialityLevel | None = None,
) -> RequiredItem:
    """TODO or REJECTED -> SUBMITTED, with an attachment if given or required.

    ``upload`` (a new file) and ``document`` (an existing document of the thesis)
    are exclusive. The file goes through vault.store / add_version in its own
    transaction first; then the item is locked, checked again and submitted.
    """
    if upload is not None and document is not None:
        raise ValueError("upload and document are exclusive")
    fresh = RequiredItem.objects.select_related("thesis", "document").get(pk=item.pk)
    try:
        _require_writer(user, fresh)
        if document is not None and document.thesis_id != fresh.thesis_id:
            raise _DeniedError("foreign_document")
        if document is not None and not can_access_document(user, document):
            raise _DeniedError("unreadable_document")
    except _DeniedError as denied:
        _audit_denial(user, "submit", denied.reason, obj=fresh)
        raise PermissionDenied from None
    if fresh.thesis.status not in OPEN_THESIS:
        raise FrozenFileError(_("The thesis is finished: its administrative file is frozen."))
    target(fresh.status, Event.SUBMIT)  # early: nothing stored for an impossible submission
    allowed = allowed_levels(fresh)
    if document is not None:
        if ConfidentialityLevel(document.confidentiality) not in allowed:
            raise ValidationError(_("A validator could not read this document."), code="level")
        if RequiredItem.objects.filter(document=document).exclude(pk=fresh.pk).exists():
            raise ValidationError(_("This document belongs to another item."), code="document")
    if upload is not None:
        if fresh.document is not None:
            if level is not None:
                raise ValueError("the level of an attached document is changed in Documents")
            vault.add_version(document=fresh.document, upload=upload, user=user)
            document = fresh.document
        else:
            chosen = ConfidentialityLevel(level or fresh.default_level)
            if chosen not in allowed:
                raise ValidationError(
                    _("A validator could not read the file at this level."), code="level"
                )
            document = vault.store(
                thesis=fresh.thesis,
                upload=upload,
                title=_document_title(fresh),
                level=chosen,
                user=user,
            )

    def work(locked: RequiredItem) -> RequiredItem:
        _require_writer(user, locked)
        before = locked.status
        locked.status = target(before, Event.SUBMIT)
        attached = document or locked.document
        if attached is not None:
            if attached.thesis_id != locked.thesis_id:  # checked again under lock
                raise _DeniedError("foreign_document")
            if locked.document_id not in (None, attached.pk):
                raise InvalidTransitionError("another document was attached meanwhile")
            locked.document = attached
            locked.submitted_version = Document.objects.get(pk=attached.pk).current_version
        elif locked.attachment_required:
            raise ValidationError(_("This item needs an attachment."), code="attachment")
        locked.cycle += 1
        locked.submitted_by = user
        locked.reason_code = ""
        locked.reason_comment = ""
        try:
            with transaction.atomic():  # savepoint: a UNIQUE(document) race
                locked.save()
        except IntegrityError:
            raise ValidationError(
                _("This document belongs to another item."), code="document"
            ) from None
        extra = {"version": locked.submitted_version} if locked.document_id else {}
        audit(
            AuditAction.ADMIN_ITEM_SUBMITTED,
            actor=user,
            obj=locked,
            **_transition_details(locked, before, **extra),
        )
        return locked

    return _run(fresh, user, "submit", work)


def withdraw(*, item: RequiredItem, user: User) -> RequiredItem:
    """SUBMITTED -> TODO by the student; this cycle's approvals no longer count."""

    def work(locked: RequiredItem) -> RequiredItem:
        _require_writer(user, locked)
        before = locked.status
        locked.status = target(before, Event.WITHDRAW)
        locked.save()
        audit(
            AuditAction.ADMIN_ITEM_WITHDRAWN,
            actor=user,
            obj=locked,
            **_transition_details(locked, before),
        )
        return locked

    return _run(item, user, "withdraw", work)


# --------------------------------------------------------------------------- decide


def _all_mandatory_validated(thesis: Thesis) -> bool:
    statuses = list(
        RequiredItem.objects.filter(thesis=thesis, mandatory=True).values_list("status", flat=True)
    )
    return bool(statuses) and all(s == ItemStatus.VALIDATED for s in statuses)


def approve(*, item: RequiredItem, user: User) -> RequiredItem:
    """One validator role approves the cycle; VALIDATED once every role has.

    The last mandatory validation of a SETUP thesis activates it, in the same
    transaction (THESIS_ACTIVATED audited last).
    """

    def work(locked: RequiredItem) -> RequiredItem:
        roles = _require_decider(user, locked)
        before = locked.status
        final = target(before, Event.APPROVE)
        if locked.document_id is not None:
            attached = Document.objects.get(pk=locked.document_id)
            if not can_access_document(user, attached):
                raise _DeniedError("unreadable_document")
            if attached.current_version != locked.submitted_version:
                raise StaleSubmissionError(_("The attachment changed since it was submitted."))
        done = set(
            ItemApproval.objects.filter(item=locked, cycle=locked.cycle).values_list(
                "role", flat=True
            )
        )
        submitter = locked.submitted_by
        if submitter is None:  # impossible once SUBMITTED (database constraint)
            raise InvalidTransitionError("a submitted item has a submitter")
        free = [r for r in roles if r not in done]
        if not free:
            raise _DeniedError("already_approved")
        try:
            with transaction.atomic():  # savepoint: unique (item, cycle, user)
                ItemApproval.objects.create(
                    item=locked,
                    cycle=locked.cycle,
                    role=free[0],
                    user=user,
                    submitted_by=submitter,
                    version=locked.submitted_version,
                    created_by=user,
                )
        except IntegrityError:
            # One approval per PERSON and cycle: a user holding both roles has
            # already approved with the other one (database constraint).
            raise _DeniedError("already_approved") from None
        complete = {Role(r) for r in locked.validator_roles} <= {*done, free[0]}
        activated = False
        if complete:
            locked.status = final
            locked.validated_at = timezone.now()
            locked.save()
            thesis = locked.thesis
            if thesis.status == Thesis.Status.SETUP and _all_mandatory_validated(thesis):
                thesis.status = Thesis.Status.ACTIVE
                thesis.save(update_fields=["status", "updated_at"])
                activated = True
        audit(
            AuditAction.ADMIN_ITEM_APPROVED,
            actor=user,
            obj=locked,
            role=free[0].value,
            cycle=locked.cycle,
            version=locked.submitted_version,
            complete=complete,
        )
        if complete:
            audit(
                AuditAction.ADMIN_ITEM_VALIDATED,
                actor=user,
                obj=locked,
                **_transition_details(locked, before),
            )
        if activated:
            activation: dict[str, Any] = {"from": "SETUP", "to": "ACTIVE"}
            audit(
                AuditAction.THESIS_ACTIVATED,
                actor=user,
                obj=locked.thesis,
                **activation,
            )
        return locked

    return _run(item, user, "approve", work)


def reject(*, item: RequiredItem, user: User, reason: Any, comment: str = "") -> RequiredItem:
    """SUBMITTED -> REJECTED by a validator role, with a reason (code audited only)."""
    code, comment = _clean_reason(reason, comment)

    def work(locked: RequiredItem) -> RequiredItem:
        _require_decider(user, locked)
        before = locked.status
        locked.status = target(before, Event.REJECT)
        locked.reason_code, locked.reason_comment = code, comment
        locked.save()
        audit(
            AuditAction.ADMIN_ITEM_REJECTED,
            actor=user,
            obj=locked,
            **_transition_details(locked, before, reason=code),
        )
        return locked

    return _run(item, user, "reject", work)


def reopen(*, item: RequiredItem, user: User, reason: Any, comment: str = "") -> RequiredItem:
    """VALIDATED -> TODO by the student or a validator role, with a reason.

    An ACTIVE thesis stays ACTIVE (SETUP -> ACTIVE is one-way in M3); the event
    says whether the item is mandatory, for the "incomplete file" banner.
    """
    code, comment = _clean_reason(reason, comment)

    def work(locked: RequiredItem) -> RequiredItem:
        _require_sees(user, locked)
        if not (can(user, Action.ADMIN_FILE_WRITE, locked) or _validator_roles_of(user, locked)):
            raise _DeniedError("not_validator")
        before = locked.status
        locked.status = target(before, Event.REOPEN)
        locked.validated_at = None
        locked.reason_code, locked.reason_comment = code, comment
        locked.save()
        audit(
            AuditAction.ADMIN_ITEM_REOPENED,
            actor=user,
            obj=locked,
            **_transition_details(locked, before, reason=code, mandatory=locked.mandatory),
        )
        return locked

    return _run(item, user, "reopen", work)
