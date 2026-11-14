"""services.create_thesis: what `manage.py create_thesis` (and demo_seed) call."""

import datetime as dt
from typing import Any

import pytest
from django.core import mail

from apps.accounts import services
from apps.accounts.models import Invitation, Organization, Thesis
from apps.accounts.roles import Role
from apps.admin_file.models import RequiredItem
from apps.objectives.models import ThesisYear
from apps.vault.models import AuditEvent

pytestmark = pytest.mark.django_db

KWARGS: dict[str, Any] = {
    "title": "Thèse fictive",
    "cifre_start": dt.date(2026, 3, 1),
    "first_enrollment": dt.date(2026, 3, 15),
    "planned_end": dt.date(2029, 2, 28),
    "student_email": "Doctorant.Fictif@Example.org",
    "student_organization": "Entreprise fictive",
    "student_organization_kind": Organization.Kind.ST,
}


def test_creates_thesis_file_years_and_sends_the_invitation() -> None:
    inv = services.create_thesis(**KWARGS)
    thesis = Thesis.objects.get()
    assert (inv.thesis, inv.role, inv.email) == (
        thesis,
        Role.PHD_STUDENT,
        "doctorant.fictif@example.org",
    )
    assert inv.email_status == Invitation.EmailStatus.SENT
    assert len(mail.outbox) == 1
    assert RequiredItem.objects.filter(thesis=thesis).exists()
    assert ThesisYear.objects.filter(thesis=thesis).exists()
    assert inv.organization == Organization.objects.get(name="Entreprise fictive")
    actions = list(AuditEvent.objects.order_by("seq").values_list("action", flat=True))
    assert actions[0] == "THESIS_CREATED"
    assert "INVITATION_CREATED" in actions
    assert actions[-1] == "INVITATION_EMAIL_SENT"


def test_refuses_end_before_start_and_creates_nothing() -> None:
    with pytest.raises(services.ThesisCreationError, match="planned end"):
        services.create_thesis(**{**KWARGS, "planned_end": dt.date(2025, 1, 1)})
    assert not Thesis.objects.exists()
    assert not AuditEvent.objects.exists()


@pytest.mark.without_vault_keys
def test_refuses_before_vault_init() -> None:
    with pytest.raises(services.ThesisCreationError, match="vault_init"):
        services.create_thesis(**KWARGS)
    assert not Thesis.objects.exists()


def test_invalid_student_address_rolls_everything_back() -> None:
    with pytest.raises(services.TeamError):
        services.create_thesis(**{**KWARGS, "student_email": "not an address"})
    assert not Thesis.objects.exists()
    assert not Organization.objects.exists()
    assert not AuditEvent.objects.exists()
