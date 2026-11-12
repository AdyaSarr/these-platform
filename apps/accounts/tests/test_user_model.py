"""accounts.User: e-mail is the identifier, UUID primary key, Argon2 hashes."""

import secrets
import uuid

import pytest
from django.contrib.auth import authenticate, get_user_model
from django.core.exceptions import FieldDoesNotExist
from django.db import IntegrityError, models, transaction

from apps.accounts.models import User
from apps.accounts.tests.factories import UserFactory


def test_get_user_model_is_accounts_user() -> None:
    assert get_user_model() is User


def test_email_is_the_identifier() -> None:
    assert User.USERNAME_FIELD == "email"
    assert User.EMAIL_FIELD == "email"
    assert User.REQUIRED_FIELDS == []
    with pytest.raises(FieldDoesNotExist):
        User._meta.get_field("username")  # type: ignore[misc]  # absence is the point


def test_primary_key_is_uuid() -> None:
    assert isinstance(User._meta.pk, models.UUIDField)
    assert isinstance(User().pk, uuid.UUID)


@pytest.mark.django_db
def test_create_user_normalizes_email() -> None:
    user = User.objects.create_user(email="  Alice.Martin@Example.ORG ")
    assert user.email == "alice.martin@example.org"
    assert str(user) == "alice.martin@example.org"


@pytest.mark.django_db
def test_create_user_requires_email() -> None:
    with pytest.raises(ValueError, match="email"):
        User.objects.create_user(email="")


@pytest.mark.django_db
def test_create_user_without_password_is_unusable() -> None:
    user = User.objects.create_user(email="nopw@example.org")
    assert user.has_usable_password() is False


@pytest.mark.django_db
def test_defaults() -> None:
    user = User.objects.create_user(email="defaults@example.org")
    assert user.is_active is True
    assert user.is_staff is False
    assert user.is_superuser is False
    assert user.date_joined is not None


@pytest.mark.django_db
def test_email_unique_case_insensitive() -> None:
    User.objects.create_user(email="dup@example.org")
    with transaction.atomic(), pytest.raises(IntegrityError):
        # bulk_create bypasses save() and the manager: the database must refuse.
        User.objects.bulk_create([User(email="DUP@example.org")])


@pytest.mark.django_db
def test_password_hashed_with_argon2() -> None:
    raw = secrets.token_urlsafe(16)
    user = UserFactory.create(password=raw)
    assert user.password.startswith("argon2")
    assert user.check_password(raw)


@pytest.mark.django_db
def test_authenticate_with_email_any_case() -> None:
    raw = secrets.token_urlsafe(16)
    user = UserFactory.create(email="bob@example.org", password=raw)
    assert authenticate(username="BOB@Example.org", password=raw) == user
    assert authenticate(username="bob@example.org", password=secrets.token_urlsafe()) is None


@pytest.mark.django_db
def test_create_superuser() -> None:
    admin = User.objects.create_superuser(
        email="root@example.org", password=secrets.token_urlsafe()
    )
    assert admin.is_staff is True
    assert admin.is_superuser is True


@pytest.mark.django_db
def test_create_superuser_rejects_non_staff() -> None:
    with pytest.raises(ValueError, match="is_staff"):
        User.objects.create_superuser(email="x@example.org", password="x", is_staff=False)  # noqa: S106
