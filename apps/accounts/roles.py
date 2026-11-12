"""Roles a user can hold on a thesis (docs/ARCHITECTURE.md).

Kept apart from models.py and permissions.py so both can import it without a cycle.
"""

from django.db import models
from django.utils.translation import gettext_lazy as _


class Role(models.TextChoices):
    PHD_STUDENT = "PHD_STUDENT", _("PhD student")
    DIRECTOR = "DIRECTOR", _("Thesis director")
    CO_SUPERVISOR = "CO_SUPERVISOR", _("Co-supervisor")
    INDUSTRIAL_TUTOR = "INDUSTRIAL_TUTOR", _("Industrial tutor")
    CSI_MEMBER = "CSI_MEMBER", _("CSI member")
    GUEST = "GUEST", _("Guest")


# Roles a CSI member may not hold on the same thesis at the same time (charte I2S:
# the CSI is independent from the supervision). Enforced by a database constraint.
CSI_INCOMPATIBLE_ROLES: frozenset[Role] = frozenset(
    {Role.PHD_STUDENT, Role.DIRECTOR, Role.CO_SUPERVISOR, Role.INDUSTRIAL_TUTOR}
)
