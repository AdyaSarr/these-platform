"""An invitation accepted without a password (demo_seed): no sign-in is possible
until the person chooses one through the password-reset link, and the second
factor is then enrolled as for any new account."""

import ast
import re
import secrets
from pathlib import Path
from typing import Any

import pytest
from django.contrib.auth import authenticate
from django.core import mail
from django.test import Client
from django.urls import reverse

from apps.accounts import services
from apps.accounts.models import Invitation, Membership, User
from apps.accounts.roles import Role
from apps.accounts.tests.factories import MembershipFactory, OrganizationFactory

pytestmark = pytest.mark.django_db

LINK = re.compile(r"/invitation/([A-Za-z0-9_-]{43})/")
SITE_ORIGIN = "http://testserver"
EMAIL = "sans.mot.de.passe@example.org"


def _csrf_token(response: Any) -> str:
    match = re.search(r'name="csrfmiddlewaretoken" value="([^"]+)"', response.content.decode())
    assert match, "no CSRF token in the page"
    return match.group(1)


@pytest.fixture
def account(django_capture_on_commit_callbacks: Any) -> User:
    student = MembershipFactory.create(role=Role.PHD_STUDENT)
    mail.outbox.clear()
    with django_capture_on_commit_callbacks(execute=True):
        inv = services.create_invitation(
            thesis=student.thesis,
            email=EMAIL,
            role=Role.DIRECTOR,
            organization=OrganizationFactory.create(),
            by=student.user,
        )
    token = LINK.findall(str(mail.outbox[-1].body))[0]
    assert services.check_token(token).pk == inv.pk
    mail.outbox.clear()
    return services.accept_invitation_new_user(
        inv.pk, first_name="Prénom", last_name="Fictif", password=None
    )


def test_account_has_no_usable_password_and_a_membership(account: User) -> None:
    assert not account.has_usable_password()
    assert Membership.objects.filter(user=account, role=Role.DIRECTOR).exists()
    assert Invitation.objects.get(accepted_by=account).used_at is not None


@pytest.mark.parametrize("attempt", ["", "!", "None", "password"])
def test_account_without_usable_password_never_authenticates(account: User, attempt: str) -> None:
    assert authenticate(email=EMAIL, password=attempt) is None
    stored = account.password
    assert authenticate(email=EMAIL, password=stored) is None  # not even its own hash


def test_sign_in_form_refuses_it(account: User) -> None:
    browser = Client(enforce_csrf_checks=True)
    page = browser.get(reverse("account_login"))
    response = browser.post(
        reverse("account_login"),
        {"login": EMAIL, "password": "", "csrfmiddlewaretoken": _csrf_token(page)},
        HTTP_ORIGIN=SITE_ORIGIN,
    )
    assert response.status_code == 200  # form shown again, no step 2
    assert "_auth_user_id" not in browser.session
    assert response.headers.get("Location") != reverse("mfa_authenticate")


def _reset_path(body: str) -> str:
    link = re.search(r"https?://\S+", body).group(0)  # type: ignore[union-attr]
    return "/" + link.split("/", 3)[3]


def _choose_password_then_sign_in(path: str) -> Client:
    """Reset link -> new password -> sign-in: lands on second-factor enrolment."""
    browser = Client(enforce_csrf_checks=True)
    page = browser.get(path, follow=True)
    assert page.status_code == 200
    form_path = page.redirect_chain[-1][0] if page.redirect_chain else path
    new = secrets.token_urlsafe(16)
    done = browser.post(
        form_path,
        {"password1": new, "password2": new, "csrfmiddlewaretoken": _csrf_token(page)},
        HTTP_ORIGIN=SITE_ORIGIN,
    )
    assert done.status_code == 302, done.content[:300]
    assert "_auth_user_id" not in browser.session  # a reset never signs in
    page = browser.get(reverse("account_login"))
    signed = browser.post(
        reverse("account_login"),
        {"login": EMAIL, "password": new, "csrfmiddlewaretoken": _csrf_token(page)},
        HTTP_ORIGIN=SITE_ORIGIN,
    )
    assert signed.status_code == 302
    # Signed in, no factor yet: every page sends to the enrolment of one.
    assert "_auth_user_id" in browser.session
    home = browser.get("/")
    assert home.status_code == 302
    assert home["Location"] == reverse("mfa_index")
    return browser


def test_forgotten_password_works_for_it(
    account: User, django_capture_on_commit_callbacks: Any
) -> None:
    """allauth's ResetPasswordForm does not skip unusable passwords (Django's own
    PasswordResetForm does): the ordinary "forgotten password" page works."""
    browser = Client(enforce_csrf_checks=True)
    page = browser.get(reverse("account_reset_password"))
    with django_capture_on_commit_callbacks(execute=True):
        browser.post(
            reverse("account_reset_password"),
            {"email": EMAIL, "csrfmiddlewaretoken": _csrf_token(page)},
            HTTP_ORIGIN=SITE_ORIGIN,
        )
    assert [m.to for m in mail.outbox] == [[EMAIL]]
    _choose_password_then_sign_in(_reset_path(str(mail.outbox[0].body)))
    account.refresh_from_db()
    assert account.has_usable_password()


def test_the_view_always_passes_the_form_password() -> None:
    """Only a caller outside the views (demo_seed) may pass password=None."""
    source = Path(services.__file__).with_name("invitation_views.py").read_text()
    calls = [
        node
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "accept_invitation_new_user"
    ]
    assert calls
    for call in calls:
        password = next(k.value for k in call.keywords if k.arg == "password")
        assert ast.unparse(password) == "form.cleaned_data['password1']"
