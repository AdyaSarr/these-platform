"""Administrative file (M3): the required items of a thesis and their approvals.

Every rule below is ALSO checked by admin_file.services before writing; the
database constraints are the last line of defence (a bug, a shell, a future
module) and are tested on their own (tests/test_models.py).
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from types import MappingProxyType
from typing import Any, ClassVar, TypeVar

from django.contrib.postgres.fields import ArrayField
from django.db import models
from django.db.models import F, Q
from django.utils.translation import gettext_lazy as _

from apps.accounts.permissions import Action
from apps.accounts.roles import Role
from apps.core.confidentiality import ConfidentialityLevel
from apps.core.models import TimeStampedModel


class Section(models.TextChoices):
    """Who SEES an item: the CIFRE part (tutor included) or the academic part."""

    CIFRE = "CIFRE", _("CIFRE")
    ACADEMIC = "ACADEMIC", _("Academic")


# The read right of each section (matrix of docs/ARCHITECTURE.md: the tutor reads
# "partie CIFRE" only). Also the condition to read an item's attachment.
SECTION_READ_ACTION: Mapping[Section, Action] = MappingProxyType(
    {Section.CIFRE: Action.CIFRE_FILE_READ, Section.ACADEMIC: Action.ADMIN_FILE_READ}
)

# The only validator sets, each with ONE spelling: no duplicate (an item that
# could never be validated), never the student or a co-supervisor.
VALIDATOR_ROLES: tuple[tuple[Role, ...], ...] = (
    (Role.DIRECTOR,),
    (Role.INDUSTRIAL_TUTOR,),
    (Role.DIRECTOR, Role.INDUSTRIAL_TUTOR),
)
APPROVER_ROLES = (Role.DIRECTOR, Role.INDUSTRIAL_TUTOR)

CATALOGUE_CODE_REGEX = r"^[a-z][a-z0-9_]{0,63}$"
CUSTOM_CODE_REGEX = r"^custom-[0-9a-f]{12}$"


class ItemStatus(models.TextChoices):
    TODO = "TODO", _("To do")
    SUBMITTED = "SUBMITTED", _("Submitted")
    VALIDATED = "VALIDATED", _("Validated")
    REJECTED = "REJECTED", _("Rejected")


class Reason(models.TextChoices):
    """Closed list of reasons (reject, reopen); only the code is audited."""

    WRONG_DOCUMENT = "WRONG_DOCUMENT", _("Wrong document")
    ILLEGIBLE = "ILLEGIBLE", _("Illegible")
    MISSING_SIGNATURE = "MISSING_SIGNATURE", _("Missing signature")
    EXPIRED = "EXPIRED", _("Expired")
    AMENDMENT = "AMENDMENT", _("Amendment")
    OTHER = "OTHER", _("Other")


class DueDateReason(models.TextChoices):
    """Why the student proposes another due date (closed list; the code is audited)."""

    WAITING_SIGNATURE = "WAITING_SIGNATURE", _("Waiting for a signature")
    WAITING_THIRD_PARTY = (
        "WAITING_THIRD_PARTY",
        _("Waiting for a third party (ANRT, doctoral school, company)"),
    )
    ADMINISTRATIVE_DELAY = "ADMINISTRATIVE_DELAY", _("Administrative delay")
    AMENDMENT = "AMENDMENT", _("Amendment in progress")
    OTHER = "OTHER", _("Other")


class HistoryError(Exception):
    """A history row (reopening, decided due-date proposal) is never modified or deleted."""


_M = TypeVar("_M", bound=models.Model)


class _HistoryQuerySet(models.QuerySet[_M]):
    """No bulk update or delete of a history (tests/test_histories.py)."""

    def update(self, **kwargs: Any) -> int:
        raise HistoryError("a history is never updated in bulk")

    def delete(self) -> tuple[int, dict[str, int]]:
        raise HistoryError("a history is never deleted")

    def bulk_update(self, *args: Any, **kwargs: Any) -> int:
        raise HistoryError("a history is never updated in bulk")

    def _raw_delete(self, *args: Any, **kwargs: Any) -> int:
        raise HistoryError("a history is never deleted")


class RequiredItem(TimeStampedModel):
    """One piece of the administrative file of a thesis.

    Catalogue items (admin_file.catalog) have an empty ``label``: it is shown
    from the catalogue by ``code``. Custom items have a ``custom-<12 hex>`` code
    and a clear-text label (warned on the form, never in the audit log).
    """

    Status = ItemStatus
    Reason = Reason

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    thesis = models.ForeignKey(
        "accounts.Thesis",
        on_delete=models.PROTECT,
        related_name="required_items",
        editable=False,
    )
    code = models.CharField(max_length=64, editable=False)
    label = models.CharField(_("label"), max_length=120, blank=True)
    section = models.CharField(_("section"), max_length=10, choices=Section.choices)
    validator_roles = ArrayField(
        models.CharField(max_length=20, choices=Role.choices), verbose_name=_("validated by")
    )
    default_level = models.CharField(
        _("default level"), max_length=20, choices=ConfidentialityLevel.choices
    )
    mandatory = models.BooleanField(_("mandatory"), default=True)
    attachment_required = models.BooleanField(_("attachment required"), default=True)
    due_date = models.DateField(_("due date"), null=True, blank=True)
    status = models.CharField(
        _("status"), max_length=10, choices=Status.choices, default=Status.TODO, editable=False
    )
    # Incremented by each submission; approvals belong to one cycle.
    cycle = models.PositiveIntegerField(default=0, editable=False)
    document = models.OneToOneField(
        "vault.Document",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="required_item",
        editable=False,
    )
    # The version approved is the version submitted: a later version (added from
    # the Documents page) is never validated by an approval of this cycle.
    submitted_version = models.PositiveBigIntegerField(null=True, editable=False)
    submitted_by = models.ForeignKey(
        "accounts.User", on_delete=models.PROTECT, null=True, related_name="+", editable=False
    )
    validated_at = models.DateTimeField(null=True, editable=False)
    # When the current cycle was deposited: « en attente de validation depuis X jours »
    # (frontend step 2). Cleared by a withdrawal, a rejection or a reopening; NULL
    # for the items deposited before the field existed (no number is shown).
    submitted_at = models.DateTimeField(null=True, editable=False)
    reason_code = models.CharField(
        max_length=20, choices=Reason.choices, blank=True, editable=False
    )
    reason_comment = models.CharField(max_length=280, blank=True, editable=False)

    class Meta:
        verbose_name = _("required item")
        verbose_name_plural = _("required items")
        ordering: ClassVar[list[str]] = ["thesis", "due_date", "code"]
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.UniqueConstraint(fields=["thesis", "code"], name="admin_file_item_code_unique"),
            # A mandatory catalogue item always has a due date (custom items: the
            # student's rule); existing rows filled in by migration 0004.
            models.CheckConstraint(
                condition=~(Q(code__regex=CATALOGUE_CODE_REGEX) & Q(mandatory=True))
                | Q(due_date__isnull=False),
                name="admin_file_item_catalogue_due_date",
            ),
            models.CheckConstraint(
                condition=(Q(code__regex=CATALOGUE_CODE_REGEX) & Q(label=""))
                | (Q(code__regex=CUSTOM_CODE_REGEX) & ~Q(label="")),
                name="admin_file_item_code_label",
            ),
            models.CheckConstraint(
                condition=Q(validator_roles__in=[list(r) for r in VALIDATOR_ROLES]),
                name="admin_file_item_validator_roles",
            ),
            models.CheckConstraint(
                condition=Q(section__in=Section.values), name="admin_file_item_known_section"
            ),
            models.CheckConstraint(
                condition=Q(default_level__in=ConfidentialityLevel.values),
                name="admin_file_item_known_level",
            ),
            models.CheckConstraint(
                condition=Q(status__in=ItemStatus.values), name="admin_file_item_known_status"
            ),
            models.CheckConstraint(
                condition=Q(reason_code="") | Q(reason_code__in=Reason.values),
                name="admin_file_item_known_reason",
            ),
            models.CheckConstraint(
                condition=(Q(status=ItemStatus.VALIDATED) & Q(validated_at__isnull=False))
                | (~Q(status=ItemStatus.VALIDATED) & Q(validated_at__isnull=True)),
                name="admin_file_item_validated_at",
            ),
            models.CheckConstraint(
                condition=~Q(status=ItemStatus.REJECTED) | ~Q(reason_code=""),
                name="admin_file_item_rejected_reason",
            ),
            models.CheckConstraint(
                condition=Q(status=ItemStatus.TODO)
                | (Q(cycle__gte=1) & Q(submitted_by__isnull=False)),
                name="admin_file_item_submitted_cycle",
            ),
            models.CheckConstraint(
                condition=Q(document__isnull=True)
                | ~Q(status__in=[ItemStatus.SUBMITTED, ItemStatus.VALIDATED])
                | Q(submitted_version__isnull=False),
                name="admin_file_item_submitted_version",
            ),
        ]

    def __str__(self) -> str:
        # Never the label: str() ends up in admin pages, logs and error reports.
        return f"required item {self.pk}"


class ItemApproval(TimeStampedModel):
    """One validator's approval of one submission cycle of an item.

    Separation of duties, enforced here as well as in the services: one approval
    per role AND one per person in a cycle (a user holding both validator roles
    cannot validate a two-party item alone), and never by the cycle's submitter
    (``submitted_by`` is copied from the item at approval time).
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    item = models.ForeignKey(
        RequiredItem, on_delete=models.PROTECT, related_name="approvals", editable=False
    )
    cycle = models.PositiveIntegerField(editable=False)
    role = models.CharField(max_length=20, choices=Role.choices, editable=False)
    user = models.ForeignKey(
        "accounts.User", on_delete=models.PROTECT, related_name="+", editable=False
    )
    submitted_by = models.ForeignKey(
        "accounts.User", on_delete=models.PROTECT, related_name="+", editable=False
    )
    version = models.PositiveBigIntegerField(null=True, editable=False)

    class Meta:
        verbose_name = _("approval")
        verbose_name_plural = _("approvals")
        ordering: ClassVar[list[str]] = ["item", "cycle", "created_at"]
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.UniqueConstraint(
                fields=["item", "cycle", "role"], name="admin_file_approval_one_per_role"
            ),
            models.UniqueConstraint(
                fields=["item", "cycle", "user"], name="admin_file_approval_one_per_user"
            ),
            models.CheckConstraint(
                condition=~Q(user=F("submitted_by")), name="admin_file_approval_not_submitter"
            ),
            models.CheckConstraint(
                condition=Q(role__in=APPROVER_ROLES), name="admin_file_approval_validator_role"
            ),
            models.CheckConstraint(condition=Q(cycle__gte=1), name="admin_file_approval_cycle"),
        ]

    def __str__(self) -> str:
        return f"approval {self.pk}"


# Spelled out (not .values): a constraint must not change when an enum does.
_SEND_STATUSES = ["PENDING", "SENT", "FAILED", "SKIPPED"]


class ReminderLog(TimeStampedModel):
    """One reminder e-mail: (item, kind, due date, recipient) is claimed ONCE.

    The unique constraint is what makes concurrent runs of the reminder command
    (timer and manual run) send each reminder once: the run that inserts the
    row queues the e-mail, the other one finds it. A new due date gives new rows.
    """

    class Kind(models.TextChoices):
        J14 = "J14", _("14 days before")
        J3 = "J3", _("3 days before")
        OVERDUE = "OVERDUE", _("Overdue")

    class Status(models.TextChoices):
        PENDING = "PENDING", _("Pending")
        SENT = "SENT", _("Sent")
        FAILED = "FAILED", _("Failed")
        SKIPPED = "SKIPPED", _("Not sent: no longer concerned or due")

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    item = models.ForeignKey(
        RequiredItem, on_delete=models.PROTECT, related_name="reminders", editable=False
    )
    kind = models.CharField(max_length=10, choices=Kind.choices, editable=False)
    due_date = models.DateField(editable=False)
    recipient = models.ForeignKey(
        "accounts.User", on_delete=models.PROTECT, related_name="+", editable=False
    )
    status = models.CharField(
        max_length=10, choices=Status.choices, default=Status.PENDING, editable=False
    )
    attempts = models.PositiveSmallIntegerField(default=0, editable=False)
    sent_at = models.DateTimeField(null=True, editable=False)

    class Meta:
        verbose_name = _("reminder")
        verbose_name_plural = _("reminders")
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.UniqueConstraint(
                fields=["item", "kind", "due_date", "recipient"],
                name="admin_file_reminder_once",
            ),
            models.CheckConstraint(
                condition=Q(kind__in=["J14", "J3", "OVERDUE"]), name="admin_file_reminder_kind"
            ),
            models.CheckConstraint(
                condition=Q(status__in=_SEND_STATUSES), name="admin_file_reminder_status"
            ),
            models.CheckConstraint(
                condition=(Q(status="SENT") & Q(sent_at__isnull=False))
                | (~Q(status="SENT") & Q(sent_at__isnull=True)),
                name="admin_file_reminder_sent_at",
            ),
        ]

    def __str__(self) -> str:
        return f"reminder {self.pk}"


# Spelled out (not .values): a constraint must not change when an enum does.
_PROPOSAL_STATUSES = ["PENDING", "ACCEPTED", "REFUSED", "WITHDRAWN", "SUPERSEDED"]
_NOTIFICATION_KINDS = ["REOPENED", "DUE_PROPOSED", "DUE_ACCEPTED", "DUE_REFUSED", "DUE_CHANGED"]


class ItemReopening(TimeStampedModel):
    """One reopened cycle of an item: who, when, why, and when it had been validated.

    History: written once by services.reopen(), never modified nor deleted.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    item = models.ForeignKey(
        RequiredItem, on_delete=models.PROTECT, related_name="reopenings", editable=False
    )
    cycle = models.PositiveIntegerField(editable=False)
    reopened_by = models.ForeignKey(
        "accounts.User", on_delete=models.PROTECT, related_name="+", editable=False
    )
    reason_code = models.CharField(max_length=20, choices=Reason.choices, editable=False)
    reason_comment = models.CharField(max_length=280, blank=True, editable=False)
    validated_at = models.DateTimeField(editable=False)

    objects: ClassVar[models.Manager[ItemReopening]] = _HistoryQuerySet.as_manager()

    class Meta:
        verbose_name = _("reopening")
        verbose_name_plural = _("reopenings")
        ordering: ClassVar[list[str]] = ["item", "cycle"]
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.UniqueConstraint(fields=["item", "cycle"], name="admin_file_reopening_once"),
            models.CheckConstraint(condition=Q(cycle__gte=1), name="admin_file_reopening_cycle"),
            models.CheckConstraint(
                condition=Q(reason_code__in=Reason.values), name="admin_file_reopening_reason"
            ),
        ]

    def save(self, *args: Any, **kwargs: Any) -> None:
        if not self._state.adding:
            raise HistoryError("a reopening is never modified")
        super().save(*args, **kwargs)

    def delete(self, *args: Any, **kwargs: Any) -> tuple[int, dict[str, int]]:
        raise HistoryError("a reopening is never deleted")

    def __str__(self) -> str:
        return f"reopening {self.pk}"


class DueDateProposal(TimeStampedModel):
    """A due date proposed by the student for a catalogue item, decided by a validator.

    PENDING is changed once (accepted, refused, withdrawn, superseded by a direct
    change), by the services, on the locked row; a decided proposal is history:
    never modified, and no proposal is ever deleted. The comment is clear text
    (warned on the form), never in the audit log.
    """

    class Status(models.TextChoices):
        PENDING = "PENDING", _("Pending")
        ACCEPTED = "ACCEPTED", _("Accepted")
        REFUSED = "REFUSED", _("Refused")
        WITHDRAWN = "WITHDRAWN", _("Withdrawn")
        SUPERSEDED = "SUPERSEDED", _("Superseded")

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    item = models.ForeignKey(
        RequiredItem, on_delete=models.PROTECT, related_name="due_date_proposals", editable=False
    )
    proposed_by = models.ForeignKey(
        "accounts.User", on_delete=models.PROTECT, related_name="+", editable=False
    )
    from_date = models.DateField(null=True, editable=False)  # the due date when proposed
    to_date = models.DateField(editable=False)
    reason_code = models.CharField(max_length=30, choices=DueDateReason.choices, editable=False)
    comment = models.CharField(max_length=280, blank=True, editable=False)
    status = models.CharField(
        max_length=12, choices=Status.choices, default=Status.PENDING, editable=False
    )
    decided_by = models.ForeignKey(
        "accounts.User", on_delete=models.PROTECT, null=True, related_name="+", editable=False
    )
    decided_at = models.DateTimeField(null=True, editable=False)

    objects: ClassVar[models.Manager[DueDateProposal]] = _HistoryQuerySet.as_manager()

    class Meta:
        verbose_name = _("due-date proposal")
        verbose_name_plural = _("due-date proposals")
        ordering: ClassVar[list[str]] = ["item", "created_at"]
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.UniqueConstraint(
                fields=["item"], condition=Q(status="PENDING"), name="admin_file_proposal_pending"
            ),
            models.CheckConstraint(
                condition=Q(status__in=_PROPOSAL_STATUSES), name="admin_file_proposal_status"
            ),
            models.CheckConstraint(
                condition=Q(reason_code__in=DueDateReason.values),
                name="admin_file_proposal_reason",
            ),
            models.CheckConstraint(
                condition=(
                    Q(status="PENDING") & Q(decided_at__isnull=True) & Q(decided_by__isnull=True)
                )
                | (
                    Q(status__in=["ACCEPTED", "REFUSED", "SUPERSEDED"])
                    & Q(decided_at__isnull=False)
                    & Q(decided_by__isnull=False)
                )
                | (
                    Q(status="WITHDRAWN") & Q(decided_at__isnull=False) & Q(decided_by__isnull=True)
                ),
                name="admin_file_proposal_decision",
            ),
            models.CheckConstraint(
                condition=Q(decided_by__isnull=True) | ~Q(decided_by=F("proposed_by")),
                name="admin_file_proposal_not_self_decided",
            ),
        ]

    def save(self, *args: Any, **kwargs: Any) -> None:
        """A row changes only while it is PENDING in the database (the services
        hold its lock: the status read here is the one they decide on)."""
        if not self._state.adding:
            stored = type(self).objects.filter(pk=self.pk).values_list("status", flat=True).first()
            if stored != self.Status.PENDING:
                raise HistoryError("a decided due-date proposal is never modified")
        super().save(*args, **kwargs)

    def delete(self, *args: Any, **kwargs: Any) -> tuple[int, dict[str, int]]:
        raise HistoryError("a due-date proposal is never deleted")

    def __str__(self) -> str:
        # Never the comment: str() ends up in admin pages, logs and error reports.
        return f"due-date proposal {self.pk}"


class ItemNotification(TimeStampedModel):
    """One e-mail about an event of an item (reopening, due date): a link only.

    The reminders' mechanism (ReminderLog), for events: queued after commit with
    its id only, sent once (the task locks the row), retried by the daily run up
    to reminders.MAX_ATTEMPTS. ``event_id`` names the event (a reopening, a
    proposal, or one direct change of the due date).
    """

    class Kind(models.TextChoices):
        REOPENED = "REOPENED", _("Item reopened")
        DUE_PROPOSED = "DUE_PROPOSED", _("Due date proposed")
        DUE_ACCEPTED = "DUE_ACCEPTED", _("Proposed due date accepted")
        DUE_REFUSED = "DUE_REFUSED", _("Proposed due date refused")
        DUE_CHANGED = "DUE_CHANGED", _("Due date changed")

    Status = ReminderLog.Status

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    item = models.ForeignKey(
        RequiredItem, on_delete=models.PROTECT, related_name="notifications", editable=False
    )
    kind = models.CharField(max_length=15, choices=Kind.choices, editable=False)
    event_id = models.UUIDField(editable=False)
    recipient = models.ForeignKey(
        "accounts.User", on_delete=models.PROTECT, related_name="+", editable=False
    )
    status = models.CharField(
        max_length=10, choices=Status.choices, default=Status.PENDING, editable=False
    )
    attempts = models.PositiveSmallIntegerField(default=0, editable=False)
    sent_at = models.DateTimeField(null=True, editable=False)

    class Meta:
        verbose_name = _("item notification")
        verbose_name_plural = _("item notifications")
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.UniqueConstraint(
                fields=["kind", "event_id", "recipient"], name="admin_file_notification_once"
            ),
            models.CheckConstraint(
                condition=Q(kind__in=_NOTIFICATION_KINDS), name="admin_file_notification_kind"
            ),
            models.CheckConstraint(
                condition=Q(status__in=_SEND_STATUSES), name="admin_file_notification_status"
            ),
            models.CheckConstraint(
                condition=(Q(status="SENT") & Q(sent_at__isnull=False))
                | (~Q(status="SENT") & Q(sent_at__isnull=True)),
                name="admin_file_notification_sent_at",
            ),
        ]

    def __str__(self) -> str:
        return f"item notification {self.pk}"
