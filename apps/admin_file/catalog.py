"""Catalogue of the required items, created for each thesis by services.seed_items().

In code, not in a data migration: no thesis exists when migrations run. Adding
an entry here and running `manage.py admin_file_seed --all` adds it to the
existing theses; an existing item is never modified from here.

Table validated in the M3 plan; tests/test_catalog.py holds an independent
transcription. Due dates are fictitious defaults, editable by the student.
Labels are clear text (never personal data); English msgids, French UI.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from enum import StrEnum

from django.utils.functional import Promise
from django.utils.translation import gettext_lazy as _

from apps.accounts.models import Thesis
from apps.accounts.roles import Role
from apps.core.confidentiality import ConfidentialityLevel

from .models import Section


class DueFrom(StrEnum):
    CIFRE_START = "cifre_start_date"
    FIRST_ENROLLMENT = "first_enrollment_date"


@dataclass(frozen=True)
class CatalogItem:
    code: str
    label: str | Promise
    section: Section
    validator_roles: tuple[Role, ...]
    default_level: ConfidentialityLevel
    mandatory: bool
    attachment_required: bool
    due_from: DueFrom
    due_offset_days: int
    deposit_warning: str | Promise = ""

    def due_date(self, thesis: Thesis) -> dt.date:
        base: dt.date = getattr(thesis, self.due_from.value)
        return base + dt.timedelta(days=self.due_offset_days)


_D, _T = Role.DIRECTOR, Role.INDUSTRIAL_TUTOR
_L = ConfidentialityLevel
_CIFRE, _ACAD = Section.CIFRE, Section.ACADEMIC
_START, _ENROL = DueFrom.CIFRE_START, DueFrom.FIRST_ENROLLMENT

# fmt: off
CATALOG: tuple[CatalogItem, ...] = (
    # Salary inside: INTERNAL_ST (the LIRMM director cannot read it). The tutor
    # can: attachment optional, an employment certificate is preferred.
    CatalogItem(
        "employment_contract", _("ST employment contract"), _CIFRE,
        (_T,), _L.INTERNAL_ST, True, False, _START, 0,
        _("The industrial tutor will be able to read this file: prefer an employment "
          "certificate to the contract itself (salary)."),
    ),
    CatalogItem(
        "cifre_agreement", _("CIFRE agreement (ANRT)"), _CIFRE,
        (_T,), _L.INTERNAL_ST, True, True, _START, 30,
    ),
    # Two parties, two validations: each supervisor vouches for their side's
    # signed copy. CONFIDENTIAL: both validators read it, co-supervisors do not.
    CatalogItem(
        "collaboration_contract", _("ST-LIRMM collaboration contract"), _CIFRE,
        (_D, _T), _L.CONFIDENTIAL, True, True, _START, 90,
    ),
    # The doctoral school requires a minimum salary: the director reads it.
    CatalogItem(
        "funding_proof", _("Proof of funding"), _CIFRE,
        (_D,), _L.CONFIDENTIAL, True, True, _ENROL, 0,
        _("Certificate stating salary ≥ doctoral school threshold, without the amount."),
    ),
    # LIRMM procedure; the notice only, never the application file.
    CatalogItem(
        "fsd_zrr_clearance", _("FSD notice / ZRR access"), _ACAD,
        (_D,), _L.INTERNAL_LIRMM, True, False, _START, 0,
        _("Attach the notice only, never the application file (passport, nationality)."),
    ),
    CatalogItem(
        "adum_enrollment", _("ADUM enrolment"), _ACAD,
        (_D,), _L.INTERNAL_LIRMM, True, True, _ENROL, 0,
    ),
    CatalogItem(
        "doctoral_charter", _("Signed doctoral charter"), _ACAD,
        (_D,), _L.INTERNAL_LIRMM, True, True, _ENROL, 30,
    ),
    CatalogItem(
        "training_agreement", _("Individual training agreement"), _ACAD,
        (_D,), _L.INTERNAL_LIRMM, True, True, _ENROL, 90,
    ),
    CatalogItem(
        "training_plan", _("Individual training plan"), _ACAD,
        (_D,), _L.INTERNAL_LIRMM, True, True, _ENROL, 180,
    ),
    CatalogItem(
        "cvec_certificate", _("CVEC certificate"), _ACAD,
        (_D,), _L.INTERNAL_LIRMM, True, True, _ENROL, 0,
    ),
)
# fmt: on


def by_code(code: str) -> CatalogItem | None:
    return next((item for item in CATALOG if item.code == code), None)
