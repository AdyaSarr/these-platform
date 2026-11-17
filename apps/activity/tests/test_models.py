"""The four models of the activity app (M11a): random UUID keys (requirement a), no
free text on an event, uniqueness of reads and of notifications."""

from __future__ import annotations

import uuid

import pytest
from django.db import IntegrityError, models, transaction

from apps.activity.models import ActivityNotification, Event, EventRead, NotificationPreference

pytestmark = pytest.mark.django_db

MODELS = [Event, EventRead, NotificationPreference, ActivityNotification]


@pytest.mark.parametrize("model", MODELS, ids=lambda m: m.__name__)
def test_the_primary_key_is_a_random_uuid(model: type[models.Model]) -> None:
    """Requirement a: never a sequential integer; the gap between two ids says
    nothing (uuid4, 122 random bits)."""
    pk = model._meta.pk
    assert isinstance(pk, models.UUIDField)
    assert pk.default is uuid.uuid4


def test_an_event_holds_no_title_no_level_no_free_text() -> None:
    names = {f.name for f in Event._meta.get_fields() if getattr(f, "concrete", False)}
    assert names == {
        "id", "created_at", "updated_at", "thesis", "kind", "object_type", "object_id",
        "actor",
    }  # fmt: skip
    for field in Event._meta.concrete_fields:
        if isinstance(field, models.CharField):
            assert field.choices, field.name  # closed lists only


def test_a_read_is_recorded_once(django_user_model: object) -> None:
    from apps.activity.tests.helpers import make_team

    team = make_team()
    event = Event.objects.create(
        thesis=team.thesis,
        kind="DOCUMENT_ADDED",
        object_type="DOCUMENT",
        object_id=uuid.uuid4(),
        actor=team.student,
    )
    EventRead.objects.create(event=event, user=team.director)
    with pytest.raises(IntegrityError), transaction.atomic():
        EventRead.objects.create(event=event, user=team.director)


def test_one_preference_per_person_thesis_and_category() -> None:
    from apps.activity.tests.helpers import make_team

    team = make_team()
    NotificationPreference.objects.create(
        user=team.director, thesis=team.thesis, category="INFO", frequency="DAILY"
    )
    with pytest.raises(IntegrityError), transaction.atomic():
        NotificationPreference.objects.create(
            user=team.director, thesis=team.thesis, category="INFO", frequency="WEEKLY"
        )
