"""Confidentiality levels (CLAUDE.md rule 5).

Defined in core because accounts (access rules) and vault (M2, documents) both
need them and neither should depend on the other for it.
"""

from django.db import models
from django.utils.translation import gettext_lazy as _


class ConfidentialityLevel(models.TextChoices):
    PUBLIC = "PUBLIC", _("Public")
    INTERNAL_LIRMM = "INTERNAL_LIRMM", _("Internal LIRMM")
    INTERNAL_ST = "INTERNAL_ST", _("Internal ST")
    CONFIDENTIAL = "CONFIDENTIAL", _("Confidential")
