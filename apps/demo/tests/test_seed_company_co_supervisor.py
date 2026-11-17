"""demo_seed: a fifth fictitious person, the company co-supervisor
(INDUSTRIAL_CO_SUPERVISOR), invited with the rest of the team, attending the
meeting with the tutor (INTERNAL_ST) and commenting an objective."""

from __future__ import annotations

from typing import Any

import pytest

from apps.accounts.models import Membership, Organization, Thesis
from apps.accounts.roles import Role
from apps.demo import scenario
from apps.meetings.models import Attendee
from apps.objectives.models import ObjectiveComment

pytestmark = [pytest.mark.django_db(transaction=True), pytest.mark.without_vault_keys]

ICS = Role.INDUSTRIAL_CO_SUPERVISOR


@pytest.fixture
def demo(settings: Any) -> scenario.SeedResult:
    settings.DEBUG = True
    return scenario.seed()


def test_the_fifth_person_is_fictitious_and_on_the_company_side() -> None:
    (person,) = [p for p in scenario.PEOPLE if p.role is ICS]
    assert person.email.endswith("@example.org")
    assert person.organization == scenario.COMPANY
    assert len(scenario.PEOPLE) == 6
    assert len({p.email for p in scenario.PEOPLE}) == 6


def test_the_company_co_supervisor_joins_the_demo_thesis(demo: scenario.SeedResult) -> None:
    thesis = Thesis.objects.get(title=scenario.DEMO_TITLE)
    membership = Membership.objects.active().get(thesis=thesis, role=ICS)
    assert membership.organization.kind == Organization.Kind.ST
    user = membership.user
    # The meeting with the tutor is the only ST_TUTOR one (its title is sealed).
    tutor_meeting = Attendee.objects.filter(
        meeting__thesis=thesis, meeting__kind=scenario.TUTOR_MEETING["kind"]
    )
    assert user.pk in set(tutor_meeting.values_list("user_id", flat=True))
    assert ObjectiveComment.objects.filter(thesis=thesis, author=user).exists()
