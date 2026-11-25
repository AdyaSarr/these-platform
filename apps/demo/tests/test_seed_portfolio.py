"""demo_seed, trainings (M9a): fictitious trainings of several categories and levels,
one to come, a certificate in the vault, a re-registration date; written through
the services only, each at its simulated instant (checked by test_seed.py)."""

from __future__ import annotations

from typing import Any

import pytest

from apps.accounts.models import Membership, Thesis, User
from apps.accounts.roles import Role
from apps.demo import scenario
from apps.portfolio import services
from apps.portfolio.models import Training

pytestmark = [pytest.mark.django_db(transaction=True), pytest.mark.without_vault_keys]


@pytest.fixture
def demo(settings: Any) -> scenario.SeedResult:
    settings.DEBUG = True
    return scenario.seed()


def _thesis() -> Thesis:
    return Thesis.objects.get(title=scenario.DEMO_TITLE)


def _user(role: Role) -> User:
    return Membership.objects.active().get(thesis=_thesis(), role=role).user


def test_trainings_of_several_kinds(demo: scenario.SeedResult) -> None:
    trainings = Training.objects.filter(thesis=_thesis())
    assert {"INTEGRITY", "SCIENTIFIC", "TRANSVERSAL"} <= set(
        trainings.values_list("category", flat=True)
    )
    assert {"PUBLIC", "INTERNAL_ST"} <= set(trainings.values_list("level", flat=True))
    assert trainings.filter(certificate__isnull=False).exists()
    listing = services.training_list(_user(Role.PHD_STUDENT), _thesis())
    assert listing.integrity_done and listing.upcoming and listing.total_hours > 0
    assert services.policy_of(_thesis()).reregistration_date is not None


def test_the_csi_member_sees_the_public_trainings_only(demo: scenario.SeedResult) -> None:
    listing = services.training_list(_user(Role.CSI_MEMBER), _thesis())
    assert listing.done and all(r.training.level == "PUBLIC" for r in listing.done)
    assert listing.partial and listing.integrity_done


def test_m9b_fictitious_portfolio(demo: scenario.SeedResult) -> None:
    """M9b: a seminar without edition, a poster, publication details of the accepted
    paper, teaching and outreach of several levels, the student's skills and
    professional project."""
    from apps.conferences.models import Submission, Venue
    from apps.portfolio.models import CareerNote, Outreach, PublicationDetails, SkillNote, Teaching

    thesis = _thesis()
    assert Venue.objects.filter(thesis=thesis, kind="SEMINAR").exists()
    assert Submission.objects.filter(thesis=thesis, presentation_kind="POSTER").exists()
    assert PublicationDetails.objects.filter(thesis=thesis).exists()
    assert {"PUBLIC", "INTERNAL_ST"} <= set(
        Teaching.objects.filter(thesis=thesis).values_list("level", flat=True)
    )
    assert Outreach.objects.filter(thesis=thesis).exists()
    assert SkillNote.objects.filter(thesis=thesis).exists()
    assert CareerNote.objects.filter(thesis=thesis).exists()
    student = _user(Role.PHD_STUDENT)
    assert services.career_note(student, thesis)
    assert services.teaching_list(_user(Role.INDUSTRIAL_CO_SUPERVISOR), thesis).done
