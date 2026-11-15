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
