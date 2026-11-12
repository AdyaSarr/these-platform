"""Acceptance: sign-in, sign-out and role changes are recorded in the audit log."""

import datetime as dt
import secrets
from typing import Any

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
    client.post(reverse("account_login"), {"login": "Nobody@Example.org", "password": wrong})
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


# --------------------------------------------------------------------------- second factor


def test_mfa_events_are_audited_without_secrets(django_capture_on_commit_callbacks: Any) -> None:
    import datetime as dt

    import time_machine
    from allauth.mfa.recovery_codes.internal.auth import RecoveryCodes

    from apps.accounts.tests.factories import totp_code
    from apps.accounts.tests.helpers import mfa_login, password_login

    user = UserFactory.create(password=PASSWORD)
    codes = RecoveryCodes.activate(user).get_unused_codes()
    t0 = dt.datetime(2026, 12, 1, 10, 0, tzinfo=dt.UTC)
    with time_machine.travel(t0):
        client = Client()
        mfa_login(client, user, PASSWORD)  # TOTP used
        other = Client()
        password_login(other, user, PASSWORD)
        other.post(reverse("mfa_authenticate"), {"code": "000000"})  # failure (or replay)
        third = Client()
        password_login(third, user, PASSWORD)
        third.post(reverse("mfa_authenticate"), {"code": codes[0]})  # recovery code used
    with time_machine.travel(t0 + dt.timedelta(minutes=1)):
        secret: str = user.totp_secret  # type: ignore[attr-defined]  # set by the factory
        client.post(reverse("mfa_reauthenticate"), {"code": totp_code(secret)})
        # allauth sends the reset signal on commit
        with django_capture_on_commit_callbacks(execute=True):
            client.post(reverse("mfa_generate_recovery_codes"))  # reset
    events = list(AuditEvent.objects.filter(actor=user).values_list("action", "details_json"))
    actions = [a for a, _ in events]
    assert ("MFA_FACTOR_USED", {"type": "totp", "reauthenticated": False}) in events
    assert ("MFA_FACTOR_USED", {"type": "recovery_codes", "reauthenticated": False}) in events
    assert ("MFA_FACTOR_USED", {"type": "totp", "reauthenticated": True}) in events
    assert "MFA_FAILED" in actions
    assert ("MFA_FACTOR_RESET", {"type": "recovery_codes"}) in events
    dump = str(list(AuditEvent.objects.values()))
    assert secret not in dump
    assert not any(code in dump for code in codes)


def test_totp_enrollment_is_audited(django_capture_on_commit_callbacks: Any) -> None:
    """Real enrollment page: a fresh account adds its TOTP and the event is logged."""
    from allauth.mfa.totp.internal.auth import SECRET_SESSION_KEY

    from apps.accounts.reauth import record_password_authentication  # noqa: F401  (flow under test)
    from apps.accounts.tests.factories import totp_code

    user = UserFactory.create(password=PASSWORD, mfa=False)
    client = Client()
    client.post(reverse("account_login"), {"login": user.email, "password": PASSWORD})
    assert client.get(reverse("mfa_activate_totp")).status_code in (200, 302)
    secret = client.session.get(SECRET_SESSION_KEY)
    if secret is None:  # allauth asked for a re-auth first: give the password
        client.post(reverse("account_reauthenticate"), {"password": PASSWORD})
        client.get(reverse("mfa_activate_totp"))
        secret = client.session[SECRET_SESSION_KEY]
    with django_capture_on_commit_callbacks(execute=True):  # signal sent on commit
        response = client.post(reverse("mfa_activate_totp"), {"code": totp_code(secret)})
    assert response.status_code == 302
    # Enrolling TOTP also creates the recovery codes: two additions, both logged.
    added = AuditEvent.objects.filter(action="MFA_FACTOR_ADDED", actor=user)
    assert sorted(e.details_json["type"] for e in added) == ["recovery_codes", "totp"]
    assert secret not in str(list(AuditEvent.objects.values()))
