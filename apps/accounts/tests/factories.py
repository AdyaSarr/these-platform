"""Factories producing fictitious data only (CLAUDE.md rule 10)."""

import datetime as dt
import secrets
from typing import Any

import factory

from apps.accounts.models import Membership, Organization, Thesis, User
from apps.accounts.roles import Role


class UserFactory(factory.django.DjangoModelFactory[User]):
    """Use ``UserFactory.create(...)``: it is typed and returns a ``User``."""

    class Meta:
        model = User
        skip_postgeneration_save = True

    email = factory.Sequence(lambda n: f"user{n}@example.org")
    first_name = factory.Sequence(lambda n: f"Prenom{n}")
    last_name = factory.Sequence(lambda n: f"Nom{n}")

    @factory.post_generation
    def password(self: User, create: bool, extracted: str | None, **kwargs: Any) -> None:
        self.set_password(extracted or secrets.token_urlsafe(16))
        if create:
            self.save(update_fields=["password"])


class OrganizationFactory(factory.django.DjangoModelFactory[Organization]):
    class Meta:
        model = Organization

    name = factory.Sequence(lambda n: f"Organisation fictive {n}")
    kind = Organization.Kind.EXTERNAL


class ThesisFactory(factory.django.DjangoModelFactory[Thesis]):
    class Meta:
        model = Thesis

    title = factory.Sequence(lambda n: f"Thèse fictive {n}")
    cifre_start_date = dt.date(2026, 3, 1)
    first_enrollment_date = dt.date(2026, 3, 15)
    planned_end_date = dt.date(2029, 2, 28)
    status = Thesis.Status.ACTIVE


class MembershipFactory(factory.django.DjangoModelFactory[Membership]):
    class Meta:
        model = Membership

    user = factory.SubFactory(UserFactory)
    thesis = factory.SubFactory(ThesisFactory)
    organization = factory.SubFactory(OrganizationFactory)
    role = Role.DIRECTOR
