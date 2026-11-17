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

SUPERVISION = frozenset(
    {
        Role.DIRECTOR,
        Role.CO_SUPERVISOR,
        Role.INDUSTRIAL_TUTOR,
        Role.INDUSTRIAL_CO_SUPERVISOR,
    }
)


def default_frequency(roles: frozenset[Role]) -> Frequency | None:
    """Daily digest for the student, weekly for the supervision; « feed only » for
    the CSI member (independent and yearly: no weekly follow-up by e-mail; they may
    change it); nothing for a guest (who reads no event)."""
    if Role.PHD_STUDENT in roles:
        return Frequency.DAILY
    if roles & SUPERVISION:
        return Frequency.WEEKLY
    if Role.CSI_MEMBER in roles:
        return Frequency.FEED_ONLY
    return None


def frequency_of(user: Any, thesis: Thesis, category: str) -> Frequency | None:
    """The frequency of ``category`` for ``user`` on ``thesis`` (None: no e-mail).

    Defaults: Informations by role (default_frequency); À faire and Échéances,
    immediate (the behaviour before M11b)."""
    roles = roles_of(user, thesis)
    default = default_frequency(roles)
    if default is None:
        return None
    if category != Category.INFO:
        default = Frequency.IMMEDIATE
    stored = (
        NotificationPreference.objects.filter(
            user_id=user.pk, thesis_id=thesis.pk, category=category
        )
        .values_list("frequency", flat=True)
        .first()
    )
    return Frequency(stored) if stored else default


def set_preference(user: Any, thesis: Thesis, category: str, frequency: str) -> Frequency:
    """Store the frequency of ``category``; journaled with codes only.

    Refused (also by the database): « feed only » outside Informations, « weekly »
    for Échéances. Back to immediate, the rows waiting for a digest are released.
    """
    from apps.vault.audit import AuditAction, audit

    from .choices import allowed_frequencies
    from .routing import release

    if (
        not can(user, Action.VIEW_THESIS, thesis)
        or default_frequency(roles_of(user, thesis)) is None
    ):
        raise PermissionDenied
    if category not in Category.values or frequency not in Frequency.values:
        raise ValidationError(_("Unknown frequency."), code="frequency")
    if frequency not in allowed_frequencies(category):
        raise ValidationError(
            _("This frequency is not possible for these e-mails: they ask you to act."),
            code="frequency",
        )
    old = frequency_of(user, thesis, category)
    with transaction.atomic():
        NotificationPreference.objects.update_or_create(
            user_id=user.pk,
            thesis_id=thesis.pk,
            category=category,
            defaults={"frequency": frequency},
        )
        if frequency == Frequency.IMMEDIATE and old != Frequency.IMMEDIATE:
            release(user, thesis, category)
        audit(
            AuditAction.ACTIVITY_PREFERENCE_CHANGED,
            actor=user,
            thesis=thesis,
            category=str(category),
            old=str(old),
            new=str(frequency),
        )
    return Frequency(frequency)
