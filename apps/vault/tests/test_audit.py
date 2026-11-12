"""vault.services.audit(): signature, thesis resolution, forbidden content, append-only."""

from typing import Any

import pytest
from django.contrib.auth.models import AnonymousUser
from django.test import RequestFactory

from apps.accounts.tests.factories import MembershipFactory, ThesisFactory, UserFactory
from apps.vault import crypto
from apps.vault.models import AuditEvent
from apps.vault.services import AuditAction, AuditError, audit

pytestmark = pytest.mark.django_db

A = AuditAction


def test_minimal_event_without_actor_or_thesis() -> None:
    event = audit(A.LOGIN_FAILED)
    assert event.actor is None
    assert event.thesis is None
    assert event.action == "LOGIN_FAILED"
    assert event.details_json == {}


def test_plain_string_action_is_refused() -> None:
    with pytest.raises(AuditError):
        audit("LOGIN_FAILED")  # type: ignore[arg-type]


def test_thesis_deduced_from_object() -> None:
    m = MembershipFactory.create()
    event = audit(A.MEMBER_REMOVED, actor=m.user, obj=m)
    assert event.thesis == m.thesis
    assert event.object_type == "accounts.membership"
    assert event.object_id == str(m.pk)


def test_thesis_deduced_when_object_is_the_thesis() -> None:
    thesis = ThesisFactory.create()
    assert audit(A.MEMBERSHIP_GRANTED, obj=thesis).thesis == thesis


def test_explicit_thesis_equal_to_deduced_one_is_fine() -> None:
    m = MembershipFactory.create()
    assert audit(A.MEMBER_REMOVED, obj=m, thesis=m.thesis).thesis == m.thesis


def test_explicit_thesis_contradicting_object_is_refused() -> None:
    m = MembershipFactory.create()
    with pytest.raises(AuditError):
        audit(A.MEMBER_REMOVED, obj=m, thesis=ThesisFactory.create())
    assert not AuditEvent.objects.exists()


def test_explicit_thesis_without_object() -> None:
    thesis = ThesisFactory.create()
    event = audit(A.MEMBERSHIP_GRANTED, thesis=thesis)
    assert event.thesis == thesis
    assert event.object_type == ""


def test_anonymous_actor_is_stored_as_none() -> None:
    assert audit(A.LOGIN_FAILED, actor=AnonymousUser()).actor is None


def test_ip_is_pseudonymized() -> None:
    request = RequestFactory().get("/", REMOTE_ADDR="192.0.2.7")
    event = audit(A.LOGIN_SUCCEEDED, actor=UserFactory.create(), request=request)
    assert event.ip_hash == crypto.pseudonym("192.0.2.7", purpose="ip")
    assert "192.0.2.7" not in str(AuditEvent.objects.values().get())


def test_failed_login_email_is_stored_as_keyed_hash_of_normalized_email() -> None:
    event = audit(A.LOGIN_FAILED, subject_email="  Alice.Martin@Example.ORG ")
    expected = crypto.pseudonym("alice.martin@example.org", purpose="email")
    assert event.details_json == {"email_hmac": expected}
    row = str(AuditEvent.objects.values().get()).lower()
    assert "alice" not in row
    assert "example.org" not in row


@pytest.mark.parametrize(
    "key",
    [
        "password",
        "new_password",
        "Password1",
        "token",
        "invitation_token",
        "totp",
        "totp_code",
        "otp",
        "code",
        "recovery_code",
        "recovery_codes",
        "secret",
        "api_key",
        "session_key",
        "cookie",
        "email",
        "attempted_email",
    ],
)
def test_forbidden_detail_keys_are_refused(key: str) -> None:
    details: dict[str, Any] = {key: "x"}
    with pytest.raises(AuditError):
        audit(A.LOGIN_FAILED, **details)
    assert not AuditEvent.objects.exists()


def test_forbidden_keys_are_refused_at_any_depth() -> None:
    with pytest.raises(AuditError):
        audit(A.LOGIN_FAILED, context={"form": [{"password": "x"}]})


def test_allowed_details_are_stored() -> None:
    event = audit(A.MEMBER_ROLE_CHANGED, old_role="DIRECTOR", new_role="CO_SUPERVISOR")
    assert event.details_json == {"old_role": "DIRECTOR", "new_role": "CO_SUPERVISOR"}


def test_non_json_details_are_refused() -> None:
    with pytest.raises(AuditError):
        audit(A.MEMBER_ROLE_CHANGED, when=object())


def test_subject_email_cannot_be_smuggled_through_details() -> None:
    with pytest.raises(AuditError):
        audit(A.LOGIN_FAILED, email_hmac="forged")


# --------------------------------------------------------------------------- append-only


def test_event_cannot_be_updated() -> None:
    event = audit(A.LOGIN_FAILED)
    event.action = "LOGIN_SUCCEEDED"
    with pytest.raises(AuditError):
        event.save()


def test_event_cannot_be_deleted() -> None:
    event = audit(A.LOGIN_FAILED)
    with pytest.raises(AuditError):
        event.delete()


def test_queryset_update_and_delete_are_refused() -> None:
    audit(A.LOGIN_FAILED)
    with pytest.raises(AuditError):
        AuditEvent.objects.all().update(action="X")
    with pytest.raises(AuditError):
        AuditEvent.objects.all().delete()
    assert AuditEvent.objects.count() == 1


def test_events_are_numbered_in_order() -> None:
    first = audit(A.LOGIN_FAILED)
    second = audit(A.LOGIN_FAILED)
    assert second.seq > first.seq


# --------------------------------------------------------------------------- IP canonical form


def _ip_hash(addr: str) -> str:
    request = RequestFactory().get("/", REMOTE_ADDR=addr)
    return audit(A.LOGIN_FAILED, request=request).ip_hash


def test_two_writings_of_one_ipv6_share_a_pseudonym() -> None:
    a = _ip_hash("2001:DB8::1")
    assert a == _ip_hash("2001:0db8:0000:0000:0000:0000:0000:0001")
    assert a == crypto.pseudonym("2001:db8::1", purpose="ip")


def test_ipv4_mapped_ipv6_shares_the_ipv4_pseudonym() -> None:
    assert _ip_hash("::ffff:192.0.2.7") == _ip_hash("192.0.2.7")
    assert _ip_hash("::FFFF:c000:0207") == _ip_hash("192.0.2.7")


def test_distinct_addresses_keep_distinct_pseudonyms() -> None:
    assert _ip_hash("192.0.2.7") != _ip_hash("192.0.2.8")
    assert _ip_hash("2001:db8::1") != _ip_hash("2001:db8::2")


def test_value_that_is_not_an_ip_is_still_pseudonymized() -> None:
    event_hash = _ip_hash(" not-an-ip ")
    assert event_hash == crypto.pseudonym("not-an-ip", purpose="ip")
    assert "not-an-ip" not in str(AuditEvent.objects.values().last())
