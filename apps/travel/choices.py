"""Closed lists of the travel module (M7), shared by the models and the pure
counting code (split.py), which must not import the models."""

from __future__ import annotations

from django.db import models
from django.utils.translation import gettext_lazy as _
from django.utils.translation import pgettext_lazy


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


class Destination(models.TextChoices):
    LIRMM = "LIRMM", _("LIRMM Montpellier")
    ROUSSET = "ROUSSET", _("ST Rousset")
    CONFERENCE = "CONFERENCE", _("Conference")
    OTHER = "OTHER", _("Other place")


class Payer(models.TextChoices):
    ST = "ST", _("ST")
    LIRMM = "LIRMM", _("LIRMM")
    OTHER = "OTHER", _("Other body")


class Transport(models.TextChoices):
    """No car: the student has none (a car-sharing passenger is CARPOOL)."""

    TRAIN = "TRAIN", _("Train")
    COACH = "COACH", _("Coach")
    PLANE = "PLANE", _("Plane")
    BIKE = "BIKE", _("Bike")
    CARPOOL = "CARPOOL", _("Car sharing (passenger)")
    URBAN_TRANSIT = "URBAN_TRANSIT", _("Urban transit")


class Lodging(models.TextChoices):
    NONE = "NONE", _("None")
    HOTEL = "HOTEL", _("Hotel")
    GUESTHOUSE = "GUESTHOUSE", _("Guest house")
    PRIVATE = "PRIVATE", _("Staying with someone")
    OTHER = "OTHER", _("Other lodging")


class MissionStatus(models.TextChoices):
    PLANNED = "PLANNED", pgettext_lazy("mission status", "Planned")
    ORDER_REQUESTED = (
        "ORDER_REQUESTED",
        pgettext_lazy("mission status", "Mission approval requested"),
    )
    ORDER_GRANTED = "ORDER_GRANTED", pgettext_lazy("mission status", "Mission approval granted")
    ORDER_REFUSED = "ORDER_REFUSED", pgettext_lazy("mission status", "Mission approval refused")
    DONE = "DONE", pgettext_lazy("mission status", "Done")
    REIMBURSED = "REIMBURSED", pgettext_lazy("mission status", "Reimbursed")
    CANCELLED = "CANCELLED", pgettext_lazy("mission status", "Cancelled")


# The kind of presence day a mission fills in (none for another place).
PRESENCE_OF_DESTINATION = {
    Destination.LIRMM: PresenceKind.LIRMM,
    Destination.ROUSSET: PresenceKind.ROUSSET,
    Destination.CONFERENCE: PresenceKind.CONFERENCE,
}


class OrderDecision(models.TextChoices):
    PENDING = "PENDING", pgettext_lazy("mission approval", "Pending")
    GRANTED = "GRANTED", pgettext_lazy("mission approval", "Granted")
    REFUSED = "REFUSED", pgettext_lazy("mission approval", "Refused")
    WITHDRAWN = "WITHDRAWN", pgettext_lazy("mission approval", "Withdrawn")


class OrderRefusalReason(models.TextChoices):
    BUDGET_EXHAUSTED = "BUDGET_EXHAUSTED", _("Budget exhausted")
    NOT_JUSTIFIED = "NOT_JUSTIFIED", _("Not justified")
    MISSING_INFORMATION = "MISSING_INFORMATION", _("Missing information")
    DATES_CONFLICT = "DATES_CONFLICT", _("Dates conflict")
    WRONG_PAYER = "WRONG_PAYER", _("Wrong payer")
    OTHER = "OTHER", _("Other reason")
