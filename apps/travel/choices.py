"""Closed lists of the travel module (M7), shared by the models and the pure
counting code (split.py), which must not import the models."""

from __future__ import annotations

from django.db import models
from django.utils.translation import gettext_lazy as _


class PresenceKind(models.TextChoices):
    ROUSSET = "ROUSSET", _("ST Rousset")
    LIRMM = "LIRMM", _("LIRMM Montpellier")
    REMOTE = "REMOTE", _("Remote work")
    CONFERENCE = "CONFERENCE", _("Conference")
    LEAVE = "LEAVE", _("Leave")


class Side(models.TextChoices):
    """Which side of the CIFRE time split a day counts for."""

    ST = "ST", _("ST")
    LAB = "LAB", _("Laboratory")


class ShownKind(models.TextChoices):
    """A day as shown to one viewer (services.visible_days): a PresenceKind, or
    UNAVAILABLE, the director's view of a leave day (never the word leave)."""

    ROUSSET = PresenceKind.ROUSSET.value, PresenceKind.ROUSSET.label
    LIRMM = PresenceKind.LIRMM.value, PresenceKind.LIRMM.label
    REMOTE = PresenceKind.REMOTE.value, PresenceKind.REMOTE.label
    CONFERENCE = PresenceKind.CONFERENCE.value, PresenceKind.CONFERENCE.label
    LEAVE = PresenceKind.LEAVE.value, PresenceKind.LEAVE.label
    UNAVAILABLE = "UNAVAILABLE", _("Unavailable")
