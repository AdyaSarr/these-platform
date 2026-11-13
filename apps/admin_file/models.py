"""Administrative file (M3): the required items of a thesis and their approvals.

Every rule below is ALSO checked by admin_file.services before writing; the
database constraints are the last line of defence (a bug, a shell, a future
module) and are tested on their own (tests/test_models.py).
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from types import MappingProxyType
from typing import ClassVar

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
                condition=(Q(status="SENT") & Q(sent_at__isnull=False))
                | (~Q(status="SENT") & Q(sent_at__isnull=True)),
                name="admin_file_reminder_sent_at",
            ),
        ]

    def __str__(self) -> str:
        return f"reminder {self.pk}"
