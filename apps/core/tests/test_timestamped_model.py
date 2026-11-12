"""core.TimeStampedModel: abstract base for every business model.

Behaviour is tested on a concrete model defined with ``isolate_apps``: it lives in
a throw-away app registry, so it never needs a migration and
``makemigrations --check`` stays green. Its table is created and dropped inside
the test with the schema editor.
"""

import datetime as dt
from collections.abc import Generator
from typing import Any
from unittest import mock

import pytest
from django.apps import apps as global_apps
from django.conf import settings
from django.db import connection, models
from django.db.models import ProtectedError
from django.test.utils import isolate_apps

from apps.accounts.models import User
from apps.accounts.tests.factories import UserFactory
from apps.core.models import TimeStampedModel

T0 = dt.datetime(2026, 11, 12, 9, 0, tzinfo=dt.UTC)
T1 = dt.datetime(2026, 11, 12, 10, 30, tzinfo=dt.UTC)


@pytest.fixture
def sample_model(db: None) -> Generator[Any]:
    # Any: the model class is built at runtime, mypy cannot know its manager.
    with isolate_apps("apps.core") as isolated:
        assert isolated is not None
        # The isolated registry starts empty: register User so the lazy
        # ForeignKey "accounts.User" inherited from TimeStampedModel resolves.
        isolated.register_model("accounts", User)

        class Sample(TimeStampedModel):
            name = models.CharField(max_length=20)

            class Meta:
                app_label = "core"

        # Django's deletion collector (which enforces on_delete=PROTECT) looks up
        # relations to User in the *global* registry: expose Sample there for
        # the duration of the test only.
        global_apps.register_model("core", Sample)
        try:
            global_apps.clear_cache()
            with connection.schema_editor() as editor:
                editor.create_model(Sample)
            try:
                yield Sample
            finally:
                with connection.cursor() as cursor:
                    # PostgreSQL FK checks are deferred to commit; run them now,
                    # otherwise DROP TABLE fails with "pending trigger events".
                    cursor.execute("SET CONSTRAINTS ALL IMMEDIATE")
                with connection.schema_editor() as editor:
                    editor.delete_model(Sample)
        finally:
            # Runs even if create_model, the test or DROP TABLE fails: the global
            # registry is always restored. (The test transaction is rolled back
            # by pytest-django anyway, which also undoes the DDL on PostgreSQL.)
            del global_apps.all_models["core"]["sample"]
            global_apps.clear_cache()


def test_is_abstract() -> None:
    assert TimeStampedModel._meta.abstract is True


def test_created_by_field_definition() -> None:
    field = TimeStampedModel._meta.get_field("created_by")
    assert isinstance(field, models.ForeignKey)
    # Still the lazy string on an abstract model (resolved only on concrete ones).
    assert field.remote_field.model == settings.AUTH_USER_MODEL  # type: ignore[comparison-overlap]
    assert field.null is True
    assert field.remote_field.on_delete is models.PROTECT
    # "+": no reverse accessor on User (it would clash across many models).
    assert field.remote_field.related_name == "+"


def test_auto_now_add_and_auto_now(sample_model: Any) -> None:
    with mock.patch("django.utils.timezone.now", return_value=T0):
        obj = sample_model.objects.create(name="first")
    assert obj.created_at == T0
    assert obj.updated_at == T0

    with mock.patch("django.utils.timezone.now", return_value=T1):
        obj.name = "second"
        obj.save()
    obj.refresh_from_db()
    assert obj.created_at == T0, "created_at must never change after creation"
    assert obj.updated_at == T1


def test_created_by_may_be_null_for_system_objects(
    sample_model: Any,
) -> None:
    obj = sample_model.objects.create(name="system")
    assert obj.created_by is None


def test_deleting_an_author_is_protected(sample_model: Any) -> None:
    author = UserFactory.create()
    sample_model.objects.create(name="owned", created_by=author)
    with pytest.raises(ProtectedError):
        author.delete()
    # The supported path: deactivate, keep the history.
    author.is_active = False
    author.save(update_fields=["is_active"])
    assert sample_model.objects.get(name="owned").created_by == author
