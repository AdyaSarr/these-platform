"""The catalogue of required items, against an independent transcription of the
table validated in the M3 plan (docs/modules/M03-dossier-admin.md).

Changing a line of the catalogue must change this file too: that is the point.
"""

from __future__ import annotations

import pytest

from apps.accounts.permissions import LEVEL_ROLES, ROLE_ACTIONS, Action
from apps.accounts.roles import Role
from apps.admin_file.catalog import CATALOG, DueFrom
from apps.admin_file.models import SECTION_READ_ACTION, VALIDATOR_ROLES, Section
from apps.core.confidentiality import ConfidentialityLevel as L

D, T = Role.DIRECTOR, Role.INDUSTRIAL_TUTOR
CIFRE, ACAD = Section.CIFRE, Section.ACADEMIC
START, ENROL = DueFrom.CIFRE_START, DueFrom.FIRST_ENROLLMENT

# code: (section, validators, level, mandatory, attachment required, due from, offset)
EXPECTED = {
    "employment_contract": (CIFRE, (T,), L.INTERNAL_ST, True, False, START, 0),
    "cifre_agreement": (CIFRE, (T,), L.INTERNAL_ST, True, True, START, 30),
    "collaboration_contract": (CIFRE, (D, T), L.CONFIDENTIAL, True, True, START, 90),
    "funding_proof": (CIFRE, (D,), L.CONFIDENTIAL, True, True, ENROL, 0),
    "fsd_zrr_clearance": (ACAD, (D,), L.INTERNAL_LIRMM, True, False, START, 0),
    "adum_enrollment": (ACAD, (D,), L.INTERNAL_LIRMM, True, True, ENROL, 0),
    "doctoral_charter": (ACAD, (D,), L.INTERNAL_LIRMM, True, True, ENROL, 30),
    "training_agreement": (ACAD, (D,), L.INTERNAL_LIRMM, True, True, ENROL, 90),
    "training_plan": (ACAD, (D,), L.INTERNAL_LIRMM, True, True, ENROL, 180),
    "cvec_certificate": (ACAD, (D,), L.INTERNAL_LIRMM, True, True, ENROL, 0),
}


def test_catalogue_matches_the_validated_table() -> None:
    actual = {
        i.code: (
            i.section,
            i.validator_roles,
            i.default_level,
            i.mandatory,
            i.attachment_required,
            i.due_from,
            i.due_offset_days,
        )
        for i in CATALOG
    }
    assert actual == EXPECTED


def test_codes_are_unique_and_in_order() -> None:
    assert [i.code for i in CATALOG] == list(EXPECTED)


@pytest.mark.parametrize("item", CATALOG, ids=lambda i: i.code)
def test_validators_are_only_director_or_tutor(item: object) -> None:
    roles = item.validator_roles  # type: ignore[attr-defined]
    assert roles in VALIDATOR_ROLES
    assert Role.PHD_STUDENT not in roles
    assert Role.CO_SUPERVISOR not in roles


@pytest.mark.parametrize("item", CATALOG, ids=lambda i: i.code)
def test_every_validator_can_see_and_read_the_item(item: object) -> None:
    """A validator must never have to validate a piece they cannot open."""
    read = SECTION_READ_ACTION[item.section]  # type: ignore[attr-defined]
    for role in item.validator_roles:  # type: ignore[attr-defined]
        assert read in ROLE_ACTIONS[role], (item, role)
        assert role in LEVEL_ROLES[item.default_level], (item, role)  # type: ignore[attr-defined]


@pytest.mark.parametrize("item", CATALOG, ids=lambda i: i.code)
def test_the_student_can_deposit_every_item(item: object) -> None:
    assert Action.ADMIN_FILE_WRITE in ROLE_ACTIONS[Role.PHD_STUDENT]
    assert SECTION_READ_ACTION[item.section] in ROLE_ACTIONS[Role.PHD_STUDENT]  # type: ignore[attr-defined]
    assert Role.PHD_STUDENT in LEVEL_ROLES[item.default_level]  # type: ignore[attr-defined]


def test_section_read_actions() -> None:
    assert dict(SECTION_READ_ACTION) == {
        Section.CIFRE: Action.CIFRE_FILE_READ,
        Section.ACADEMIC: Action.ADMIN_FILE_READ,
    }


def test_salary_documents_are_not_readable_by_the_lirmm_director() -> None:
    by_code = {i.code: i for i in CATALOG}
    assert Role.DIRECTOR not in LEVEL_ROLES[by_code["employment_contract"].default_level]
    assert Role.DIRECTOR not in LEVEL_ROLES[by_code["cifre_agreement"].default_level]


def test_the_collaboration_contract_needs_both_parties() -> None:
    by_code = {i.code: i for i in CATALOG}
    assert set(by_code["collaboration_contract"].validator_roles) == {D, T}


def test_deposit_warnings() -> None:
    by_code = {i.code: i for i in CATALOG}
    assert str(by_code["funding_proof"].deposit_warning) == (
        "Attestation mentionnant salaire ≥ seuil de l'école doctorale, sans le montant."
    )
    for code in ("employment_contract", "fsd_zrr_clearance"):
        assert str(by_code[code].deposit_warning), code


def test_labels_are_translated_to_french() -> None:
    labels = {i.code: str(i.label) for i in CATALOG}
    assert labels["employment_contract"] == "Contrat de travail ST"
    assert labels["collaboration_contract"] == "Contrat de collaboration ST-LIRMM"
    assert labels["cvec_certificate"] == "Attestation CVEC"
