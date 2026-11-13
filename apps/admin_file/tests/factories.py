"""Factories for the administrative file. Fictitious data only (CLAUDE.md rule 10)."""

from __future__ import annotations

import factory

from apps.accounts.roles import Role
from apps.accounts.tests.factories import ThesisFactory, UserFactory
from apps.admin_file.models import ItemApproval, RequiredItem, Section
from apps.core.confidentiality import ConfidentialityLevel


class RequiredItemFactory(factory.django.DjangoModelFactory[RequiredItem]):
    class Meta:
        model = RequiredItem

    thesis = factory.SubFactory(ThesisFactory)
    code = factory.Sequence(lambda n: f"fictitious_item_{n}")
    section = Section.ACADEMIC
    validator_roles = factory.LazyFunction(lambda: [Role.DIRECTOR])
    default_level = ConfidentialityLevel.INTERNAL_LIRMM
    mandatory = True
    attachment_required = False


class ItemApprovalFactory(factory.django.DjangoModelFactory[ItemApproval]):
    """An approval row only: no state check (the services do that)."""

    class Meta:
        model = ItemApproval

    item = factory.SubFactory(
        RequiredItemFactory,
        status=RequiredItem.Status.SUBMITTED,
        cycle=1,
        submitted_by=factory.SubFactory(UserFactory, mfa=False),
    )
    cycle = 1
    role = Role.DIRECTOR
    user = factory.SubFactory(UserFactory, mfa=False)
    submitted_by = factory.SelfAttribute("item.submitted_by")
