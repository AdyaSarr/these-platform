"""Roles a user can hold on a thesis (docs/ARCHITECTURE.md).

Kept apart from models.py and permissions.py so both can import it without a cycle.
"""

from django.db import models
from django.utils.translation import gettext_lazy as _


class Role(models.TextChoices):
    PHD_STUDENT = "PHD_STUDENT", _("PhD student")
    DIRECTOR = "DIRECTOR", _("Thesis director")
    CO_SUPERVISOR = "CO_SUPERVISOR", _("LIRMM co-supervisor")
    INDUSTRIAL_TUTOR = "INDUSTRIAL_TUTOR", _("Industrial tutor")
    INDUSTRIAL_CO_SUPERVISOR = "INDUSTRIAL_CO_SUPERVISOR", _("Company co-supervisor")
    CSI_MEMBER = "CSI_MEMBER", _("CSI member")
    GUEST = "GUEST", _("Guest")


# Roles a CSI member may not hold on the same thesis at the same time (charte I2S:
# the CSI is independent from the supervision). Enforced by a database constraint.
CSI_INCOMPATIBLE_ROLES: frozenset[Role] = frozenset(
    {
        Role.PHD_STUDENT,
        Role.DIRECTOR,
        Role.CO_SUPERVISOR,
        Role.INDUSTRIAL_TUTOR,
        Role.INDUSTRIAL_CO_SUPERVISOR,
    }
)

# The two sides of the supervision (docs/decisions.md): one person holds roles of
# one side only on a thesis, at any instant. Enforced by a database constraint.
LAB_SIDE_ROLES: frozenset[Role] = frozenset({Role.DIRECTOR, Role.CO_SUPERVISOR})
COMPANY_SIDE_ROLES: frozenset[Role] = frozenset(
    {Role.INDUSTRIAL_TUTOR, Role.INDUSTRIAL_CO_SUPERVISOR}
)

# Longest stored value is 24 characters ("INDUSTRIAL_CO_SUPERVISOR"): every column
# that stores a role takes this many.
ROLE_MAX_LENGTH = 32
