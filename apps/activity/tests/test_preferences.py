"""Preferences per (person, thesis), category « informations » only in M11a.

Defaults: daily digest for the student, weekly for the supervisors and the CSI
member; nothing for a guest. What asks the person to act (ST approvals, mission
approvals, M3 validations, a refused piece, an assigned action) and their own
deadline reminders can never be « feed only »: refused by the SERVICE.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from django.core.exceptions import ValidationError

from apps.accounts.roles import Role
from apps.activity import services
from apps.activity.choices import Category, Frequency
from apps.activity.models import NotificationPreference
from apps.activity.tests import helpers as h
from apps.vault.models import AuditEvent

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def team() -> SimpleNamespace:
    return h.make_team()


@pytest.mark.parametrize(
    ("role", "expected"),
    [
        (Role.PHD_STUDENT, Frequency.DAILY),
        (Role.DIRECTOR, Frequency.WEEKLY),
        (Role.CO_SUPERVISOR, Frequency.WEEKLY),
        (Role.INDUSTRIAL_TUTOR, Frequency.WEEKLY),
        (Role.INDUSTRIAL_CO_SUPERVISOR, Frequency.WEEKLY),
        (Role.CSI_MEMBER, Frequency.WEEKLY),
        (Role.GUEST, None),
    ],
)
def test_the_defaults(team: SimpleNamespace, role: Role, expected: Any) -> None:
    user = getattr(team, h.ROLE_ATTRS[role])
    assert services.frequency_of(user, team.thesis, Category.INFO) == expected


def test_a_preference_is_per_thesis(team: SimpleNamespace) -> None:
    from apps.accounts.tests.factories import MembershipFactory

    other = h.make_team()
    MembershipFactory.create(thesis=other.thesis, user=team.director, role=Role.DIRECTOR)
    services.set_preference(team.director, team.thesis, Category.INFO, Frequency.IMMEDIATE)
    assert services.frequency_of(team.director, team.thesis, Category.INFO) == Frequency.IMMEDIATE
    assert services.frequency_of(team.director, other.thesis, Category.INFO) == Frequency.WEEKLY


@pytest.mark.parametrize("frequency", list(Frequency))
def test_the_action_category_cannot_be_set(team: SimpleNamespace, frequency: str) -> None:
    """M11a: what asks for an action stays immediate; never « feed only »."""
    with pytest.raises(ValidationError):
        services.set_preference(team.director, team.thesis, Category.ACTION, frequency)
    assert not NotificationPreference.objects.exists()


def test_feed_only_is_refused_for_what_cannot_be_turned_off(team: SimpleNamespace) -> None:
    for category in (Category.ACTION, Category.DEADLINE):
        with pytest.raises(ValidationError):
            services.set_preference(team.director, team.thesis, category, Frequency.FEED_ONLY)


def test_an_unknown_value_is_refused(team: SimpleNamespace) -> None:
    with pytest.raises(ValidationError):
        services.set_preference(team.director, team.thesis, Category.INFO, "HOURLY")


def test_a_non_member_cannot_set_a_preference(team: SimpleNamespace) -> None:
    from django.core.exceptions import PermissionDenied

    for user in (team.outsider, team.guest):
        with pytest.raises(PermissionDenied):
            services.set_preference(user, team.thesis, Category.INFO, Frequency.DAILY)


def test_a_change_is_journaled_with_codes_only(team: SimpleNamespace) -> None:
    services.set_preference(team.director, team.thesis, Category.INFO, Frequency.FEED_ONLY)
    event = AuditEvent.objects.filter(action="ACTIVITY_PREFERENCE_CHANGED").get()
    assert event.details_json == {"category": "INFO", "old": "WEEKLY", "new": "FEED_ONLY"}
    assert event.actor_id == team.director.pk
