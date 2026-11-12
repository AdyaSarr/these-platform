"""Authentication surface: no public sign-up, no e-mail change, no enumeration,
rate-limited sign-in, password reset by link only (no secret through Celery)."""

import re
import secrets
from typing import Any
from unittest import mock

import pytest
from allauth.account.models import EmailAddress
from django.core import mail
from django.test import Client
from django.urls import reverse

from apps.accounts.models import User
from apps.accounts.tests.factories import UserFactory

pytestmark = pytest.mark.django_db

PASSWORD = secrets.token_urlsafe(16)
CSRF = re.compile(rb'(name="csrfmiddlewaretoken" value="|"X-CSRFToken": ")[^"]*"')


def _norm(content: bytes) -> bytes:
    return CSRF.sub(b"", content)


# --------------------------------------------------------------------------- closed routes

CLOSED = [
    "/accounts/signup/",
    "/accounts/email/",
    "/accounts/confirm-email/",
    "/accounts/confirm-email/abc:def/",
    "/accounts/login/code/confirm/",
    "/accounts/reauthenticate/code/",
    "/accounts/reauthenticate/code/confirm/",
]


@pytest.mark.parametrize("path", CLOSED)
def test_closed_routes_answer_404(client: Client, path: str) -> None:
    assert client.get(path).status_code == 404
    assert client.post(path, {"email": "x@example.org", "password1": PASSWORD}).status_code == 404


@pytest.mark.parametrize("path", ["/accounts/email/"])
def test_email_management_closed_for_signed_in_users(client: Client, path: str) -> None:
    client.force_login(UserFactory.create())
    assert client.post(path, {"email": "other@example.org", "action_add": ""}).status_code == 404
    assert not EmailAddress.objects.filter(email="other@example.org").exists()


def test_signup_creates_nothing(client: Client) -> None:
    client.post("/accounts/signup/", {"email": "intruder@example.org", "password1": PASSWORD})
    assert not User.objects.filter(email="intruder@example.org").exists()


def test_adapter_closes_signup_even_if_a_route_reappeared() -> None:
    from apps.accounts.adapters import AccountAdapter

    assert AccountAdapter().is_open_for_signup(mock.Mock()) is False


def test_login_page_does_not_advertise_signup(client: Client) -> None:
    html = client.get(reverse("account_login")).content.decode()
    assert "signup" not in html and "inscri" not in html.lower()


# --------------------------------------------------------------------------- anti-enumeration


def test_login_answer_is_identical_for_unknown_and_known_addresses(client: Client) -> None:
    UserFactory.create(email="known@example.org", password=PASSWORD)
    wrong = secrets.token_urlsafe(12)
    known = Client().post(
        reverse("account_login"), {"login": "known@example.org", "password": wrong}
    )
    unknown = Client().post(
        reverse("account_login"), {"login": "nobody@example.org", "password": wrong}
    )
    assert known.status_code == unknown.status_code == 200
    assert _norm(known.content).replace(b"known@", b"X@") == _norm(unknown.content).replace(
        b"nobody@", b"X@"
    )


def test_lockout_is_identical_for_unknown_and_known_addresses() -> None:
    UserFactory.create(email="known@example.org", password=PASSWORD)
    answers: dict[str, tuple[int, bytes]] = {}
    for email in ("known@example.org", "nobody@example.org"):
        client = Client(REMOTE_ADDR=f"192.0.2.{len(answers) + 10}")
        for _ in range(6):
            last = client.post(reverse("account_login"), {"login": email, "password": "x" * 12})
        answers[email] = (last.status_code, _norm(last.content).replace(email.encode(), b"X"))
    assert answers["known@example.org"] == answers["nobody@example.org"]


def test_too_many_failures_block_even_the_right_password() -> None:
    UserFactory.create(email="known@example.org", password=PASSWORD)
    client = Client(REMOTE_ADDR="192.0.2.50")
    for _ in range(5):
        client.post(reverse("account_login"), {"login": "known@example.org", "password": "x" * 12})
    response = client.post(
        reverse("account_login"), {"login": "known@example.org", "password": PASSWORD}
    )
    # Refused at the password stage itself: the form comes back with an error,
    # with no redirect to the second-factor step (allauth's login_failed limit).
    assert response.status_code == 200
    assert "Location" not in response
    assert response.context["form"].errors
    assert "_auth_user_id" not in client.session


def test_right_password_reaches_the_second_factor_step_when_not_blocked() -> None:
    """Control for the test above: without prior failures, the same POST moves on."""
    UserFactory.create(email="known@example.org", password=PASSWORD)
    response = Client().post(
        reverse("account_login"), {"login": "known@example.org", "password": PASSWORD}
    )
    assert response.status_code == 302
    assert response["Location"] == reverse("mfa_authenticate")


def test_password_reset_answer_is_identical(django_capture_on_commit_callbacks: Any) -> None:
    UserFactory.create(email="known@example.org")
    mail.outbox.clear()
    with django_capture_on_commit_callbacks(execute=True):
        known = Client().post(reverse("account_reset_password"), {"email": "known@example.org"})
        unknown = Client().post(reverse("account_reset_password"), {"email": "nobody@example.org"})
    assert known.status_code == unknown.status_code
    assert known["Location"] == unknown["Location"]
    assert [m.to for m in mail.outbox] == [["known@example.org"]]  # nothing to the unknown one


# --------------------------------------------------------------------------- password reset


def test_reset_email_goes_through_celery_with_the_user_id_only(
    django_capture_on_commit_callbacks: Any,
) -> None:
    user = UserFactory.create(email="known@example.org")
    with (
        mock.patch("apps.accounts.tasks.send_password_reset_email.delay") as delay,
        django_capture_on_commit_callbacks(execute=True),
    ):
        Client().post(reverse("account_reset_password"), {"email": "known@example.org"})
    delay.assert_called_once_with(str(user.pk))


def test_reset_email_contains_only_a_link_and_the_link_works(
    django_capture_on_commit_callbacks: Any,
) -> None:
    user = UserFactory.create(email="known@example.org", first_name="Prenomfictif")
    mail.outbox.clear()
    with django_capture_on_commit_callbacks(execute=True):
        Client().post(reverse("account_reset_password"), {"email": "known@example.org"})
    body = str(mail.outbox[0].body)
    assert body.count("http") == 1
    assert "Prenomfictif" not in body and "known@example.org" not in body
    link = re.search(r"https?://\S+", body).group(0)  # type: ignore[union-attr]
    path = link.split("testserver", 1)[-1] if "testserver" in link else "/" + link.split("/", 3)[3]
    client = Client()
    page = client.get(path, follow=True)
    assert page.status_code == 200
    new = secrets.token_urlsafe(16)
    client.post(
        page.redirect_chain[-1][0] if page.redirect_chain else path,
        {"password1": new, "password2": new},
    )
    user.refresh_from_db()
    assert user.check_password(new)
    assert "_auth_user_id" not in client.session  # a reset never signs in (2nd factor)


def test_reset_task_ignores_unknown_or_inactive_users() -> None:
    from apps.accounts.tasks import send_password_reset_email

    inactive = UserFactory.create(is_active=False)
    mail.outbox.clear()
    send_password_reset_email(str(inactive.pk))
    send_password_reset_email("00000000-0000-0000-0000-000000000000")
    assert mail.outbox == []


# --------------------------------------------------------------------------- e-mail addresses


def test_invitation_acceptance_creates_one_verified_primary_address(
    client: Client, django_capture_on_commit_callbacks: Any
) -> None:
    from apps.accounts import services
    from apps.accounts.roles import Role
    from apps.accounts.tests.factories import MembershipFactory

    student = MembershipFactory.create(role=Role.PHD_STUDENT)
    mail.outbox.clear()
    with django_capture_on_commit_callbacks(execute=True):
        services.create_invitation(
            thesis=student.thesis,
            email="fresh@example.org",
            role=Role.DIRECTOR,
            organization=student.organization,
            by=student.user,
        )
    token = re.search(r"/invitation/([A-Za-z0-9_-]{43})/", str(mail.outbox[0].body)).group(1)  # type: ignore[union-attr]
    client.post(reverse("invitation_continue"), {"token": token})
    response = client.post(
        reverse("invitation_accept"),
        {"first_name": "P", "last_name": "N", "password1": PASSWORD, "password2": PASSWORD},
    )
    assert response["Location"] == reverse("mfa_activate_totp")
    user = User.objects.get(email="fresh@example.org")
    addresses = list(
        EmailAddress.objects.filter(user=user).values_list("email", "verified", "primary")
    )
    assert addresses == [("fresh@example.org", True, True)]
    # Enrollment is reachable right away: the password just chosen counts as a
    # fresh authentication, no second password prompt.
    assert client.get(reverse("mfa_activate_totp")).status_code == 200
