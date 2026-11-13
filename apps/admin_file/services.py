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

import datetime as dt
import unicodedata
from dataclasses import dataclass
from typing import Any

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, transaction
from django.db.models import QuerySet
from django.utils import timezone
from django.utils.translation import gettext as _

from apps.accounts.models import Thesis, User
from apps.accounts.permissions import (
    LEVEL_ROLES,
    ROLE_ACTIONS,
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
from .models import (
    SECTION_READ_ACTION,
    VALIDATOR_ROLES,
    ItemApproval,
    ItemStatus,
    Reason,
    RequiredItem,
    Section,
)
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
    "attachable_documents",
    "create_custom_item",
    "item_for",
    "item_rights",
    "label_of",
    "progress",
    "reject",
    "reopen",
    "seed_items",
    "submit",
    "update_due_date",
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


# --------------------------------------------------------------------------- pages
#
# What follows SHOWS or HIDES controls; it never authorizes. Every action above
# checks the rights again (and audits a refusal) whatever a page offered.


@dataclass(frozen=True)
class ItemRights:
    submit: bool
    withdraw: bool
    decide: bool  # approve or reject
    reopen: bool
    edit: bool  # due date
    already_approved: bool


def item_rights(user: Any, item: RequiredItem) -> ItemRights:
    writer = can(user, Action.ADMIN_FILE_WRITE, item)
    deciders = _validator_roles_of(user, item)
    mine = (
        item.cycle >= 1
        and ItemApproval.objects.filter(item=item, cycle=item.cycle, user_id=user.pk).exists()
    )
    is_submitter = item.submitted_by_id is not None and item.submitted_by_id == user.pk
    open_file = item.thesis.status in OPEN_THESIS
    status = ItemStatus(item.status)
    return ItemRights(
        submit=open_file and writer and status in (ItemStatus.TODO, ItemStatus.REJECTED),
        withdraw=open_file and writer and status == ItemStatus.SUBMITTED,
        decide=open_file
        and bool(deciders)
        and not is_submitter
        and not mine
        and status == ItemStatus.SUBMITTED,
        reopen=open_file and (writer or bool(deciders)) and status == ItemStatus.VALIDATED,
        edit=open_file and writer,
        already_approved=mine,
    )


def attachable_documents(user: Any, item: RequiredItem) -> list[Document]:
    """Documents of the item's thesis the user could attach to it (not authoritative)."""
    allowed = set(allowed_levels(item))
    candidates = Document.objects.filter(
        thesis_id=item.thesis_id, required_item__isnull=True
    ).order_by("title")
    return [
        d
        for d in candidates
        if ConfidentialityLevel(d.confidentiality) in allowed and can_access_document(user, d)
    ]


def is_stale(item: RequiredItem) -> bool:
    """SUBMITTED, with an attachment changed since the submission."""
    return (
        item.status == ItemStatus.SUBMITTED
        and item.document is not None
        and item.document.current_version != item.submitted_version
    )


@dataclass(frozen=True)
class Progress:
    validated: int
    total: int

    @property
    def percent(self) -> int:
        return round(100 * self.validated / self.total) if self.total else 100

    @property
    def complete(self) -> bool:
        return self.validated == self.total


def progress(user: Any, thesis: Thesis) -> Progress:
    """Mandatory items the user SEES: validated / all (never the hidden part)."""
    statuses = list(
        visible_items(user, thesis).filter(mandatory=True).values_list("status", flat=True)
    )
    return Progress(
        validated=sum(1 for s in statuses if s == ItemStatus.VALIDATED), total=len(statuses)
    )


def dashboard_context(request: Any, thesis: Thesis) -> dict[str, Any]:
    """Data of the dashboard block (accounts.views.DASHBOARD_BLOCKS)."""
    return {"progress": progress(request.user, thesis)}


# --------------------------------------------------------------------------- edits


LABEL_MAX = 120


def _clean_label(label: str) -> str:
    label = (label or "").strip()
    if not label:
        raise ValidationError(_("A label is required."), code="label")
    if len(label) > LABEL_MAX:
        raise ValidationError(_("The label is too long."), code="label")
    if any(unicodedata.category(c) == "Cc" for c in label):
        raise ValidationError(_("The label contains a control character."), code="label")
    return label


def check_validators(
    section: Section, validator_roles: list[Role], level: ConfidentialityLevel
) -> None:
    """A validator must SEE the item (section) and READ its attachment (level)."""
    if tuple(validator_roles) not in VALIDATOR_ROLES:
        raise ValidationError(_("These validators are not allowed."), code="validators")
    for role in validator_roles:
        if SECTION_READ_ACTION[section] not in ROLE_ACTIONS[role]:
            raise ValidationError(_("A validator would not see this section."), code="validators")
        if role not in LEVEL_ROLES[level]:
            raise ValidationError(
                _("A validator could not read the file at this level."), code="level"
            )


def create_custom_item(
    *,
    thesis: Thesis,
    user: User,
    label: str,
    section: Section,
    validator_roles: list[Role],
    default_level: ConfidentialityLevel,
    mandatory: bool,
    attachment_required: bool,
    due_date: dt.date | None,
) -> RequiredItem:
    """A custom item (student only). The label is CLEAR TEXT: never in the audit log.

    Its code derives from its own UUID primary key: no new source of randomness.
    """
    if not (
        can(user, Action.ADMIN_FILE_WRITE, thesis)
        and can(user, SECTION_READ_ACTION[section], thesis)
    ):
        _audit_denial(user, "create", "not_writer", thesis=thesis)
        raise PermissionDenied
    if thesis.status not in OPEN_THESIS:
        raise FrozenFileError(_("The thesis is finished: its administrative file is frozen."))
    label = _clean_label(label)
    section, default_level = Section(section), ConfidentialityLevel(default_level)
    roles = [Role(r) for r in validator_roles]
    check_validators(section, roles, default_level)
    item = RequiredItem(
        thesis=thesis,
        label=label,
        section=section,
        validator_roles=roles,
        default_level=default_level,
        mandatory=mandatory,
        attachment_required=attachment_required,
        due_date=due_date,
        created_by=user,
    )
    item.code = f"custom-{item.pk.hex[:12]}"
    with transaction.atomic():
        item.save(force_insert=True)
        audit(
            AuditAction.ADMIN_ITEM_CREATED,
            actor=user,
            obj=item,
            item=item.code,
            section=section.value,
            mandatory=mandatory,
        )
    return item


def update_due_date(*, item: RequiredItem, user: User, due_date: dt.date | None) -> RequiredItem:
    """The student moves an item's due date (reminders follow the new date)."""

    def work(locked: RequiredItem) -> RequiredItem:
        _require_writer(user, locked)
        locked.due_date = due_date
        locked.save(update_fields=["due_date", "updated_at"])
        audit(AuditAction.ADMIN_ITEM_UPDATED, actor=user, obj=locked, field="due_date")
        return locked

    return _run(item, user, "update", work)
