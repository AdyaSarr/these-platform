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

from apps.core.db_functions import TsTzRange
from apps.core.models import TimeStampedModel

from .managers import MembershipQuerySet, ThesisQuerySet, UserManager
from .roles import CSI_INCOMPATIBLE_ROLES, Role


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
        ACTIVE = "ACTIVE", _("Active")
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
    role = models.CharField(_("role"), max_length=20, choices=Role.choices)
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
        ]

    def __str__(self) -> str:
        return f"{self.user} - {self.get_role_display()} ({self.thesis})"
