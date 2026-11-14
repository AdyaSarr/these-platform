"""An invitation link opened while signed in with ANOTHER address.

Order of checks: the link's validity first (an invalid link keeps the generic
message, signed in or not); for a valid link only, « another address »: nothing
granted, the invitation not consumed, a clear message that never shows the invited
address, and a logout button (allauth's logout, POST with CSRF, fixed redirect).
"""

from __future__ import annotations

import datetime as dt
import html
import re
import secrets
from collections.abc import Callable
from typing import Any

import pytest
from django.core import mail
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from apps.accounts import services
from apps.accounts.models import Invitation, Membership, User
from apps.accounts.roles import Role
from apps.accounts.tests.factories import MembershipFactory, OrganizationFactory, UserFactory
from apps.vault.models import AuditEvent
from tests.test_csp_rendered_pages import _assert_csp_clean

pytestmark = pytest.mark.django_db

PASSWORD = secrets.token_urlsafe(16)
INVITED = "tuteur@example.org"
OTHER_ACCOUNT = (
    "Vous êtes connecté·e avec une autre adresse ; déconnectez-vous pour accepter cette invitation."
)
GENERIC = "ne peut pas être utilisé"
LINK = re.compile(r"/invitation/([A-Za-z0-9_-]{43})/")
SITE = "http://testserver"

Invite = Callable[..., tuple[Invitation, str]]


@pytest.fixture
def student() -> Membership:
    return MembershipFactory.create(role=Role.PHD_STUDENT)


@pytest.fixture
def invite(student: Membership, django_capture_on_commit_callbacks: Any) -> Invite:
    def _invite(email: str = INVITED) -> tuple[Invitation, str]:
        mail.outbox.clear()
        with django_capture_on_commit_callbacks(execute=True):
            inv = services.create_invitation(
                thesis=student.thesis,
                email=email,
                role=Role.INDUSTRIAL_TUTOR,
                organization=OrganizationFactory.create(),
                by=student.user,
            )
        inv.refresh_from_db()
        return inv, LINK.findall(str(mail.outbox[-1].body))[0]

    return _invite


@pytest.fixture
def director(student: Membership) -> User:
    return MembershipFactory.create(
        thesis=student.thesis, role=Role.DIRECTOR, user__email="directeur@example.org"
    ).user


def _signed_in(user: User) -> Client:
    client = Client()
    client.force_login(user)
    return client


def _text(response: Any) -> str:
    return re.sub(r"\s+", " ", html.unescape(response.content.decode()))


def _state(inv: Invitation) -> tuple[Any, ...]:
    fresh = Invitation.objects.get(pk=inv.pk)
    return (fresh.used_at, fresh.revoked_at, fresh.accepted_by_id, fresh.token_hash is not None)


def _grants() -> int:
    return AuditEvent.objects.filter(
        action__in=["MEMBERSHIP_GRANTED", "MEMBER_ROLE_CHANGED", "INVITATION_ACCEPTED"]
    ).count()


# --------------------------------------------------------------------------- valid link, other user


@pytest.mark.parametrize("invited_has_account", [False, True])
def test_the_link_page_says_another_address(
    invite: Invite, director: User, invited_has_account: bool
) -> None:
    if invited_has_account:
        UserFactory.create(email=INVITED)
    inv, token = invite()
    before, grants, memberships = _state(inv), _grants(), Membership.objects.count()
    response = _signed_in(director).get(f"/invitation/{token}/")
    text = _text(response)
    assert response.status_code == 409
    assert OTHER_ACCOUNT in text
    assert INVITED not in text  # the invited address is never shown
    assert f'action="{reverse("invitation_continue")}"' not in text  # no « Continuer » form
    assert f'action="{reverse("invitation_logout")}"' in text
    assert response["Referrer-Policy"] == "strict-origin"  # the token is in the URL
    assert "no-store" in response["Cache-Control"]
    _assert_csp_clean(response.content.decode(), "invitation_other_account")
    assert (_state(inv), _grants(), Membership.objects.count()) == (before, grants, memberships)


@pytest.mark.parametrize("invited_has_account", [False, True])
def test_the_accept_step_says_another_address_too(
    invite: Invite, director: User, invited_has_account: bool
) -> None:
    """Reached otherwise (Continuer pressed before signing in as someone else)."""
    if invited_has_account:
        UserFactory.create(email=INVITED)
    inv, token = invite()
    before, grants = _state(inv), _grants()
    for method in ("get", "post"):  # one browser session each
        client = Client()
        client.post("/invitation/", {"token": token})  # the invitation id is in the session
        client.force_login(director)  # then someone else signs in in this browser
        response = getattr(client, method)(reverse("invitation_accept"))
        text = _text(response)
        assert response.status_code == 409, method
        assert OTHER_ACCOUNT in text and INVITED not in text
        assert "invitation_id" not in client.session  # forgotten: nothing left to accept
        again = client.get(reverse("invitation_accept"))
        assert again.status_code == 400  # generic from now on: no invitation in session
    assert (_state(inv), _grants()) == (before, grants)
    assert not Membership.objects.filter(user=director, role=Role.INDUSTRIAL_TUTOR).exists()


def test_the_same_message_whether_the_invited_address_has_an_account(
    invite: Invite, director: User
) -> None:
    """No enumeration: the page is identical with or without an account."""
    _, token_without = invite("sans.compte@example.org")
    UserFactory.create(email="avec.compte@example.org")
    _, token_with = invite("avec.compte@example.org")
    client = _signed_in(director)
    pages = [client.get(f"/invitation/{t}/") for t in (token_without, token_with)]
    assert [p.status_code for p in pages] == [409, 409]
    strip = re.compile(r'(csrfmiddlewaretoken" value="|"X-CSRFToken": ")[^"]+"')
    assert strip.sub("", pages[0].content.decode()) == strip.sub("", pages[1].content.decode())


def test_the_invitation_stays_usable_by_the_right_person(invite: Invite, director: User) -> None:
    inv, token = invite()
    _signed_in(director).get(f"/invitation/{token}/")
    right = Client()
    right.post("/invitation/", {"token": token})
    response = right.post(
        reverse("invitation_accept"),
        {"first_name": "Prénom", "last_name": "Nom", "password1": PASSWORD, "password2": PASSWORD},
    )
    assert response.status_code == 302
    assert Invitation.objects.get(pk=inv.pk).used_at is not None
    assert Membership.objects.filter(user__email=INVITED, role=Role.INDUSTRIAL_TUTOR).exists()


# --------------------------------------------------------------------------- invalid link first


def _expire(inv: Invitation) -> None:
    Invitation.objects.filter(pk=inv.pk).update(expires_at=timezone.now() - dt.timedelta(hours=1))


def _revoke(inv: Invitation) -> None:
    Invitation.objects.filter(pk=inv.pk).update(revoked_at=timezone.now())


def _use(inv: Invitation) -> None:
    Invitation.objects.filter(pk=inv.pk).update(used_at=timezone.now())


@pytest.mark.parametrize(
    "spoil", [_expire, _revoke, _use, None], ids=["expired", "revoked", "used", "unknown"]
)
@pytest.mark.parametrize("signed_in_as_other", [False, True])
def test_an_invalid_link_keeps_the_generic_message(
    invite: Invite, director: User, spoil: Any, signed_in_as_other: bool
) -> None:
    inv, token = invite()
    if spoil is None:
        token = secrets.token_urlsafe(32)  # a well-formed, unknown token
    else:
        spoil(inv)
    client = _signed_in(director) if signed_in_as_other else Client()
    response = client.get(f"/invitation/{token}/")
    text = _text(response)
    assert response.status_code == 400
    assert GENERIC in text and OTHER_ACCOUNT not in text


# ---------------------------------------------------------------- same person, other spelling


def test_the_same_address_in_another_case_is_the_same_person(invite: Invite) -> None:
    """Same normalisation as account creation (case, spaces): not « another address »."""
    invited = UserFactory.create(email=INVITED)
    inv, token = invite()
    Invitation.objects.filter(pk=inv.pk).update(email="  Tuteur@Example.ORG ")
    client = _signed_in(invited)
    page = client.get(f"/invitation/{token}/")
    assert page.status_code == 200 and OTHER_ACCOUNT not in _text(page)
    client.post("/invitation/", {"token": token})
    confirm = client.get(reverse("invitation_accept"))
    assert confirm.status_code == 200 and OTHER_ACCOUNT not in _text(confirm)
    done = client.post(reverse("invitation_accept"))
    assert done.status_code == 302
    assert Membership.objects.filter(user=invited, role=Role.INDUSTRIAL_TUTOR).exists()


def test_signed_out_and_right_person_keep_their_flow(invite: Invite) -> None:
    _, token = invite()
    page = Client().get(f"/invitation/{token}/")
    assert page.status_code == 200 and reverse("invitation_continue") in page.content.decode()


# --------------------------------------------------------------------------- logout button


def _browser_logout(
    invite: Invite, user: User, *, query: str = "", data: dict[str, str] | None = None
) -> Any:
    """Like a browser: the « another address » page, then its « Se déconnecter » button."""
    _, token = invite()
    browser = Client(enforce_csrf_checks=True)
    browser.force_login(user)
    page = browser.get(f"/invitation/{token}/")
    assert page.status_code == 409
    csrf = re.search(r'name="csrfmiddlewaretoken" value="([^"]+)"', page.content.decode())
    assert csrf, "no CSRF token in the logout form"
    payload = {"csrfmiddlewaretoken": csrf.group(1), **(data or {})}
    response = browser.post(reverse("invitation_logout") + query, payload, HTTP_ORIGIN=SITE)
    return browser, response


def test_the_logout_button_signs_out_and_goes_home(invite: Invite, director: User) -> None:
    browser, response = _browser_logout(invite, director)
    assert response.status_code == 302
    assert response["Location"] == reverse("core:home")
    assert "_auth_user_id" not in browser.session
    assert AuditEvent.objects.filter(action="LOGOUT", actor=director).exists()


@pytest.mark.parametrize(
    ("query", "data"),
    [
        ("?next=/t/x/", None),
        ("", {"next": "https://evil.example/"}),
        ("?next=//evil.example", {"next": "/admin/"}),
    ],
)
def test_the_redirect_is_fixed_whatever_the_request_says(
    invite: Invite, director: User, query: str, data: dict[str, str] | None
) -> None:
    _, response = _browser_logout(invite, director, query=query, data=data)
    assert response["Location"] == reverse("core:home")


def test_the_logout_needs_a_post_with_csrf(director: User) -> None:
    client = _signed_in(director)
    assert client.get(reverse("invitation_logout")).status_code == 405
    strict = Client(enforce_csrf_checks=True)
    strict.force_login(director)
    assert strict.post(reverse("invitation_logout"), HTTP_ORIGIN=SITE).status_code == 403
    assert "_auth_user_id" in strict.session  # still signed in
