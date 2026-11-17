"""Preferences per (person, thesis) of the activity e-mails (M11a).

Only « informations » (Category.INFO) is chosen by the person in M11a. What asks
the person to act and their own deadline reminders can never be « feed only »:
the service refuses it (they stay immediate until M11b).
"""

from __future__ import annotations

from typing import Any

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.utils.translation import gettext as _

from apps.accounts.models import Thesis
from apps.accounts.permissions import Action, can, roles_of
from apps.accounts.roles import Role

from .choices import Category, Frequency
from .models import NotificationPreference

SUPERVISION_AND_CSI = frozenset(
    {
        Role.DIRECTOR,
        Role.CO_SUPERVISOR,
        Role.INDUSTRIAL_TUTOR,
        Role.INDUSTRIAL_CO_SUPERVISOR,
        Role.CSI_MEMBER,
    }
)


def default_frequency(roles: frozenset[Role]) -> Frequency | None:
    """Daily digest for the student, weekly for the supervision and the CSI;
    nothing for a guest (who reads no event)."""
    if Role.PHD_STUDENT in roles:
        return Frequency.DAILY
    if roles & SUPERVISION_AND_CSI:
        return Frequency.WEEKLY
    return None


def frequency_of(user: Any, thesis: Thesis, category: str) -> Frequency | None:
    """The frequency of ``category`` for ``user`` on ``thesis`` (None: no e-mail)."""
    roles = roles_of(user, thesis)
    default = default_frequency(roles)
    if default is None:
        return None
    if category != Category.INFO:
        return Frequency.IMMEDIATE  # M11a: what asks for an action stays immediate
    stored = (
        NotificationPreference.objects.filter(
            user_id=user.pk, thesis_id=thesis.pk, category=category
        )
        .values_list("frequency", flat=True)
        .first()
    )
    return Frequency(stored) if stored else default


def set_preference(user: Any, thesis: Thesis, category: str, frequency: str) -> Frequency:
    """Store the frequency of « informations »; journaled with codes only."""
    from apps.vault.audit import AuditAction, audit

    if (
        not can(user, Action.VIEW_THESIS, thesis)
        or default_frequency(roles_of(user, thesis)) is None
    ):
        raise PermissionDenied
    if frequency not in Frequency.values:
        raise ValidationError(_("Unknown frequency."), code="frequency")
    if category != Category.INFO:
        # Decisions, validations, assigned actions and own deadlines: never « feed
        # only », and fixed (immediate) in M11a.
        raise ValidationError(
            _("These e-mails stay immediate for now: they ask you to act."), code="category"
        )
    old = frequency_of(user, thesis, category)
    with transaction.atomic():
        NotificationPreference.objects.update_or_create(
            user_id=user.pk,
            thesis_id=thesis.pk,
            category=category,
            defaults={"frequency": frequency},
        )
        audit(
            AuditAction.ACTIVITY_PREFERENCE_CHANGED,
            actor=user,
            thesis=thesis,
            category=str(category),
            old=str(old),
            new=str(frequency),
        )
    return Frequency(frequency)
