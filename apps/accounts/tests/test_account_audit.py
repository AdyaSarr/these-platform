"""Acceptance: sign-in, sign-out and role changes are recorded in the audit log."""

import datetime as dt
import secrets

import pytest
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from apps.accounts import services
from apps.accounts.models import Membership
from apps.accounts.roles import Role
from apps.accounts.tests.factories import MembershipFactory, OrganizationFactory, UserFactory
from apps.vault import crypto
from apps.vault.models import AuditEvent

pytestmark = pytest.mark.django_db

PASSWORD = secrets.token_urlsafe(16)  # generated: no password literal in the repo


def test_successful_login_is_audited(client: Client) -> None:
    user = UserFactory.create(password=PASSWORD)
    assert client.login(email=user.email, password=PASSWORD)
    event = AuditEvent.objects.get(action="LOGIN_SUCCEEDED")
    assert event.actor == user


def test_failed_login_is_audited_without_clear_email(client: Client) -> None:
    wrong = secrets.token_urlsafe(8)
    client.post(reverse("login"), {"username": "Nobody@Example.org", "password": wrong})
    event = AuditEvent.objects.get(action="LOGIN_FAILED")
    assert event.actor is None
    assert event.details_json == {
        "email_hmac": crypto.pseudonym("nobody@example.org", purpose="email")
    }
    assert "nobody" not in str(AuditEvent.objects.values().get()).lower()


def test_logout_is_audited(client: Client) -> None:
    user = UserFactory.create()
    client.force_login(user)
    client.post(reverse("logout"))
    assert AuditEvent.objects.get(action="LOGOUT").actor == user


@pytest.fixture
def student() -> Membership:
    return MembershipFactory.create(role=Role.PHD_STUDENT)


def test_role_change_is_audited(student: Membership) -> None:
    m = MembershipFactory.create(thesis=student.thesis, role=Role.CO_SUPERVISOR)
    new = services.change_role(m, Role.DIRECTOR, by=student.user)
    event = AuditEvent.objects.get(action="MEMBER_ROLE_CHANGED")
    assert event.actor == student.user
    assert event.thesis == m.thesis
    assert event.object_id == str(new.pk)
    assert event.details_json == {
        "old_role": "CO_SUPERVISOR",
        "new_role": "DIRECTOR",
        "previous_membership": str(m.pk),
    }


def test_removal_is_audited(student: Membership) -> None:
    m = MembershipFactory.create(thesis=student.thesis, role=Role.DIRECTOR)
    services.end_membership(m, by=student.user)
    event = AuditEvent.objects.get(action="MEMBER_REMOVED")
    assert event.object_id == str(m.pk)
    assert event.details_json == {"role": "DIRECTOR"}


def test_extension_is_audited(student: Membership) -> None:
    m = MembershipFactory.create(
        thesis=student.thesis,
        role=Role.CSI_MEMBER,
        active_until=timezone.now() + dt.timedelta(days=3),
    )
    services.extend_membership(m, by=student.user, days=30)
    event = AuditEvent.objects.get(action="MEMBERSHIP_EXTENDED")
    assert event.details_json["days"] == 30


def test_grant_is_audited(student: Membership) -> None:
    m = services.add_membership(
        user=UserFactory.create(),
        thesis=student.thesis,
        role=Role.GUEST,
        organization=OrganizationFactory.create(),
    )
    event = AuditEvent.objects.get(action="MEMBERSHIP_GRANTED")
    assert event.object_id == str(m.pk)
    assert event.details_json == {"role": "GUEST"}


def test_refused_actions_leave_no_success_event(student: Membership) -> None:
    from django.core.exceptions import PermissionDenied

    m = MembershipFactory.create(thesis=student.thesis, role=Role.DIRECTOR)
    with pytest.raises(PermissionDenied):
        services.end_membership(m, by=m.user)
    assert not AuditEvent.objects.filter(action="MEMBER_REMOVED").exists()
