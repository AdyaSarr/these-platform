from __future__ import annotations

import datetime as dt
import uuid
from typing import Any, ClassVar

from django.contrib.auth.models import AbstractBaseUser, PermissionsMixin
from django.contrib.postgres.constraints import ExclusionConstraint
from django.contrib.postgres.fields import RangeBoundary, RangeOperators
from django.db import models
from django.db.models import Case, F, Q, Value, When
from django.db.models.functions import Lower
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from django.utils.translation import pgettext_lazy

from apps.core.db_functions import TsTzRange
from apps.core.models import TimeStampedModel

from .managers import MembershipQuerySet, ThesisQuerySet, UserManager
from .roles import (
    COMPANY_SIDE_ROLES,
    CSI_INCOMPATIBLE_ROLES,
    LAB_SIDE_ROLES,
    ROLE_MAX_LENGTH,
    Role,
)


class User(AbstractBaseUser, PermissionsMixin):
    """Platform user; the e-mail address is the identifier (no username).

    Users are never deleted, only deactivated (``is_active=False``).
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    # unique=True is required by Django's auth checks for USERNAME_FIELD; the
    # case-insensitive guarantee comes from the constraint on Lower("email").
    email = models.EmailField(_("email address"), max_length=254, unique=True)
    first_name = models.CharField(_("first name"), max_length=150, blank=True)
    last_name = models.CharField(_("last name"), max_length=150, blank=True)
    is_active = models.BooleanField(_("active"), default=True)
    is_staff = models.BooleanField(_("staff status"), default=False)
    date_joined = models.DateTimeField(_("date joined"), default=timezone.now)

    objects: ClassVar[UserManager] = UserManager()

    USERNAME_FIELD = "email"
    EMAIL_FIELD = "email"
    REQUIRED_FIELDS: ClassVar[list[str]] = []

    class Meta:
        verbose_name = _("user")
        verbose_name_plural = _("users")
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.UniqueConstraint(
                Lower("email"),
                name="accounts_user_email_ci_unique",
                violation_error_message=_("A user with this email address already exists."),
            ),
        ]

    def __str__(self) -> str:
        return self.email

    def save(self, *args: Any, **kwargs: Any) -> None:
        self.email = UserManager.normalize_email(self.email)
        super().save(*args, **kwargs)

    def clean(self) -> None:
        super().clean()
        self.email = UserManager.normalize_email(self.email)

    def get_full_name(self) -> str:
        return f"{self.first_name} {self.last_name}".strip()

    def get_short_name(self) -> str:
        return self.first_name


class Organization(TimeStampedModel):
    class Kind(models.TextChoices):
        ST = "ST", "STMicroelectronics"
        LIRMM = "LIRMM", "LIRMM"
        UNIVERSITY = "UNIVERSITY", _("University")
        EXTERNAL = "EXTERNAL", _("External")

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(_("name"), max_length=200, unique=True)
    kind = models.CharField(_("kind"), max_length=20, choices=Kind.choices)

    class Meta:
        verbose_name = _("organization")
        verbose_name_plural = _("organizations")
        ordering: ClassVar[list[str]] = ["name"]

    def __str__(self) -> str:
        return self.name


class Thesis(TimeStampedModel):
    class Status(models.TextChoices):
        SETUP = "SETUP", _("Setting up")
        # Own context: « En cours » for a thesis, without touching any other "Active".
        ACTIVE = "ACTIVE", pgettext_lazy("thesis status", "Active")
        DEFENDED = "DEFENDED", _("Defended")
        CLOSED = "CLOSED", _("Closed")

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    title = models.CharField(_("title"), max_length=300)
    cifre_start_date = models.DateField(_("CIFRE start date"))
    first_enrollment_date = models.DateField(_("first enrollment date"))
    planned_end_date = models.DateField(_("planned end date"))
    status = models.CharField(
        _("status"), max_length=20, choices=Status.choices, default=Status.SETUP
    )

    objects: ClassVar[ThesisQuerySet] = ThesisQuerySet.as_manager()  # type: ignore[assignment]

    class Meta:
        verbose_name = _("thesis")
        verbose_name_plural = _("theses")
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.CheckConstraint(
                condition=Q(planned_end_date__gt=F("cifre_start_date")),
                name="accounts_thesis_end_after_start",
            ),
        ]

    def __str__(self) -> str:
        return self.title

    @property
    def is_open(self) -> bool:
        """SETUP or ACTIVE: a DEFENDED or CLOSED thesis is frozen and sends nothing."""
        return self.status in (self.Status.SETUP, self.Status.ACTIVE)


def _active_period() -> TsTzRange:
    """[active_from, active_until) as a PostgreSQL range (half-open: '[)')."""
    return TsTzRange("active_from", "active_until", RangeBoundary())


class Membership(TimeStampedModel):
    """A role held by a user on a thesis over the period [active_from, active_until).

    Never deleted: removing a member or changing a role closes the period
    (docs/decisions.md), which keeps the history for the audit.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        User, on_delete=models.PROTECT, related_name="memberships", verbose_name=_("user")
    )
    thesis = models.ForeignKey(
        Thesis, on_delete=models.PROTECT, related_name="memberships", verbose_name=_("thesis")
    )
    organization = models.ForeignKey(
        Organization, on_delete=models.PROTECT, related_name="+", verbose_name=_("organization")
    )
    role = models.CharField(_("role"), max_length=ROLE_MAX_LENGTH, choices=Role.choices)
    active_from = models.DateTimeField(_("active from"), default=timezone.now)
    active_until = models.DateTimeField(_("active until"), null=True, blank=True)

    objects: ClassVar[MembershipQuerySet] = MembershipQuerySet.as_manager()  # type: ignore[assignment]

    class Meta:
        verbose_name = _("membership")
        verbose_name_plural = _("memberships")
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.CheckConstraint(
                condition=Q(active_until__isnull=True) | Q(active_until__gte=F("active_from")),
                name="accounts_membership_period_ordered",
            ),
            # "Unique (user, thesis, role) among active rows", stated on periods:
            # no two rows for the same (user, thesis, role) may overlap in time.
            ExclusionConstraint(
                name="accounts_membership_no_overlap",
                expressions=[
                    ("user", RangeOperators.EQUAL),
                    ("thesis", RangeOperators.EQUAL),
                    ("role", RangeOperators.EQUAL),
                    (_active_period(), RangeOperators.OVERLAPS),
                ],
            ),
            ExclusionConstraint(
                name="accounts_membership_one_phd_student",
                expressions=[
                    ("thesis", RangeOperators.EQUAL),
                    (_active_period(), RangeOperators.OVERLAPS),
                ],
                condition=Q(role=Role.PHD_STUDENT),
            ),
            # A CSI member must stay outside the supervision (charte I2S): for one
            # user and thesis, a CSI row and a supervision row may not overlap.
            # Both kinds get a group number; rows of different groups conflict.
            ExclusionConstraint(
                name="accounts_membership_csi_independent",
                expressions=[
                    ("user", RangeOperators.EQUAL),
                    ("thesis", RangeOperators.EQUAL),
                    (_active_period(), RangeOperators.OVERLAPS),
                    (
                        Case(
                            When(role=Role.CSI_MEMBER, then=Value(1)),
                            default=Value(0),
                            output_field=models.IntegerField(),
                        ),
                        RangeOperators.NOT_EQUAL,
                    ),
                ],
                condition=Q(role__in=sorted({Role.CSI_MEMBER, *CSI_INCOMPATIBLE_ROLES})),
            ),
            # One side of the supervision per person and thesis (docs/decisions.md):
            # a laboratory row (group 1) and a company row (group 0) of one user and
            # thesis may not overlap; rows of one side may.
            ExclusionConstraint(
                name="accounts_membership_one_supervision_side",
                expressions=[
                    ("user", RangeOperators.EQUAL),
                    ("thesis", RangeOperators.EQUAL),
                    (_active_period(), RangeOperators.OVERLAPS),
                    (
                        Case(
                            When(role__in=sorted(LAB_SIDE_ROLES), then=Value(1)),
                            default=Value(0),
                            output_field=models.IntegerField(),
                        ),
                        RangeOperators.NOT_EQUAL,
                    ),
                ],
                condition=Q(role__in=sorted(LAB_SIDE_ROLES | COMPANY_SIDE_ROLES)),
            ),
            # The PhD student holds no other role on their own thesis: a student row
            # (group 1) and any other row (group 0) of one user and thesis may not
            # overlap (they could validate their own items and objectives).
            ExclusionConstraint(
                name="accounts_membership_student_alone",
                expressions=[
                    ("user", RangeOperators.EQUAL),
                    ("thesis", RangeOperators.EQUAL),
                    (_active_period(), RangeOperators.OVERLAPS),
                    (
                        Case(
                            When(role=Role.PHD_STUDENT, then=Value(1)),
                            default=Value(0),
                            output_field=models.IntegerField(),
                        ),
                        RangeOperators.NOT_EQUAL,
                    ),
                ],
            ),
        ]

    def __str__(self) -> str:
        return f"{self.user} - {self.get_role_display()} ({self.thesis})"


class Invitation(TimeStampedModel):
    """An invitation to hold ``role`` on ``thesis``, accepted through an e-mailed link.

    The token never exists in the database: the e-mail task generates it, sends
    it, and stores only its SHA-256 (``token_hash``) with ``expires_at``. Until
    then (or if sending failed) ``token_hash`` is NULL and nothing can be accepted.
    """

    VALIDITY = dt.timedelta(hours=72)
    # A queued e-mail older than this is reported as "not sent" on the team page.
    STALLED_AFTER = dt.timedelta(minutes=10)

    class EmailStatus(models.TextChoices):
        QUEUED = "QUEUED", _("Queued")
        SENT = "SENT", _("Sent")
        FAILED = "FAILED", _("Failed")

    class DeliveryState(models.TextChoices):
        QUEUED = "QUEUED", _("Being sent")
        NOT_SENT = "NOT_SENT", _("Not sent")
        FAILED = "FAILED", _("Sending failed")
        SENT = "SENT", _("Sent, awaiting acceptance")
        EXPIRED = "EXPIRED", _("Expired")
        ACCEPTED = "ACCEPTED", _("Accepted")
        REVOKED = "REVOKED", _("Revoked")

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    thesis = models.ForeignKey(
        Thesis, on_delete=models.PROTECT, related_name="invitations", verbose_name=_("thesis")
    )
    email = models.EmailField(_("email address"), max_length=254)
    role = models.CharField(_("role"), max_length=ROLE_MAX_LENGTH, choices=Role.choices)
    organization = models.ForeignKey(
        Organization, on_delete=models.PROTECT, related_name="+", verbose_name=_("organization")
    )
    token_hash = models.CharField(max_length=64, null=True, unique=True, editable=False)  # noqa: DJ001
    expires_at = models.DateTimeField(_("expires at"), null=True, editable=False)
    email_status = models.CharField(
        max_length=10, choices=EmailStatus.choices, default=EmailStatus.QUEUED, editable=False
    )
    email_status_at = models.DateTimeField(default=timezone.now, editable=False)
    used_at = models.DateTimeField(_("used at"), null=True, editable=False)
    revoked_at = models.DateTimeField(_("revoked at"), null=True, editable=False)
    # NULL: invitation created by the system (manage.py create_thesis).
    invited_by = models.ForeignKey(
        User, on_delete=models.PROTECT, null=True, related_name="+", editable=False
    )
    accepted_by = models.ForeignKey(
        User, on_delete=models.PROTECT, null=True, related_name="+", editable=False
    )

    class Meta:
        verbose_name = _("invitation")
        verbose_name_plural = _("invitations")
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.UniqueConstraint(
                fields=["thesis", "email", "role"],
                condition=Q(used_at__isnull=True, revoked_at__isnull=True),
                name="accounts_invitation_one_open",
            ),
            models.CheckConstraint(
                condition=Q(used_at__isnull=True) | Q(revoked_at__isnull=True),
                name="accounts_invitation_used_xor_revoked",
            ),
            models.CheckConstraint(
                condition=Q(token_hash__isnull=True) | Q(expires_at__isnull=False),
                name="accounts_invitation_token_expires",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.email} - {self.get_role_display()} ({self.thesis})"

    def save(self, *args: Any, **kwargs: Any) -> None:
        self.email = UserManager.normalize_email(self.email)
        super().save(*args, **kwargs)

    @property
    def delivery_state(self) -> Invitation.DeliveryState:
        now = timezone.now()
        s = self.DeliveryState
        if self.revoked_at is not None:
            return s.REVOKED
        if self.used_at is not None:
            return s.ACCEPTED
        if self.email_status == self.EmailStatus.FAILED:
            return s.FAILED
        if self.email_status == self.EmailStatus.QUEUED:
            stalled = now - self.email_status_at >= self.STALLED_AFTER
            return s.NOT_SENT if stalled else s.QUEUED
        if self.expires_at is None or now >= self.expires_at:
            return s.EXPIRED
        return s.SENT
