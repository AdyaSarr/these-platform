"""Factories producing fictitious users only (CLAUDE.md rule 10)."""

import secrets
from typing import Any

import factory

from apps.accounts.models import User


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
