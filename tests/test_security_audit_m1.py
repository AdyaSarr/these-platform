"""M1 end-of-module security audit: one test per attack path, proving or refuting it.

1. IDOR: act on a membership or an invitation of ANOTHER thesis
2. Escalation: become PHD_STUDENT, remove the PhD student, invite a supervisor as CSI
3. Session fixation: key rotated at sign-in, after the second factor, at acceptance
4. Open redirect through ?next=
5. CSRF: every POST route refuses a request without a token
6. Rate limiting on the TOTP step itself
7. Header injection through an invitation address containing CR/LF
8. Deactivated account with a session still open
"""

import datetime as dt
import re
import secrets
from typing import Any

import pytest
import time_machine
from django.core import mail
from django.test import Client
from django.urls import URLPattern, URLResolver, get_resolver, reverse

from apps.accounts import services
from apps.accounts.models import Invitation, Membership, User
from apps.accounts.roles import Role
from apps.accounts.services import TeamError
from apps.accounts.tests.factories import MembershipFactory, UserFactory, totp_code
from apps.accounts.tests.helpers import mfa_login, password_login

pytestmark = pytest.mark.django_db

PASSWORD = secrets.token_urlsafe(16)
T0 = dt.datetime(2026, 12, 1, 10, 0, tzinfo=dt.UTC)
SAMPLE_UUID = "11111111-2222-3333-4444-555555555555"


@pytest.fixture
def student() -> Membership:
    return MembershipFactory.create(role=Role.PHD_STUDENT, user__password=PASSWORD)


@pytest.fixture
def signed_in(client: Client, student: Membership) -> Client:
    with time_machine.travel(T0):
        mfa_login(client, student.user, PASSWORD)
    return client


def _now(minutes: int = 2) -> Any:
    return time_machine.travel(T0 + dt.timedelta(minutes=minutes))


def _u(name: str, thesis_id: Any, **kw: Any) -> str:
    return reverse(f"accounts:{name}", kwargs={"thesis_id": thesis_id, **kw})


# --------------------------------------------------------------------------- 1. IDOR


@pytest.fixture
def foreign() -> dict[str, Any]:
    """Another thesis, with a director, a CSI member and an open invitation."""
    other_student = MembershipFactory.create(role=Role.PHD_STUDENT)
    thesis = other_student.thesis
    director = MembershipFactory.create(thesis=thesis, role=Role.DIRECTOR)
    csi = MembershipFactory.create(
        thesis=thesis,
        role=Role.CSI_MEMBER,
        active_from=T0 - dt.timedelta(days=1),
        active_until=T0 + dt.timedelta(days=30),
    )
    inv = services.create_invitation(
        thesis=thesis,
        email="foreign-invitee@example.org",
        role=Role.DIRECTOR,
        organization=other_student.organization,
        by=other_student.user,
    )
    return {"thesis": thesis, "director": director, "csi": csi, "invitation": inv}


def _idor_targets(f: dict[str, Any]) -> list[tuple[str, dict[str, Any], dict[str, Any]]]:
    return [
        ("team_change_role", {"membership_id": f["director"].pk}, {"role": Role.CO_SUPERVISOR}),
        ("team_remove", {"membership_id": f["director"].pk}, {}),
        ("team_extend", {"membership_id": f["csi"].pk}, {"days": 30}),
        ("team_resend_invitation", {"invitation_id": f["invitation"].pk}, {}),
        ("team_revoke_invitation", {"invitation_id": f["invitation"].pk}, {}),
    ]


def _foreign_unchanged(f: dict[str, Any]) -> None:
    f["director"].refresh_from_db()
    f["csi"].refresh_from_db()
    f["invitation"].refresh_from_db()
    assert f["director"].active_until is None
    assert f["csi"].active_until == T0 + dt.timedelta(days=30)
    assert f["invitation"].revoked_at is None
    assert Invitation.objects.filter(thesis=f["thesis"]).count() == 1


def test_idor_foreign_objects_through_own_thesis_url(
    signed_in: Client, student: Membership, foreign: dict[str, Any]
) -> None:
    """Own thesis in the URL, foreign object id: 404, nothing changes."""
    with _now():
        for name, ids, data in _idor_targets(foreign):
            response = signed_in.post(_u(name, student.thesis.pk, **ids), data)
            assert response.status_code == 404, name
    _foreign_unchanged(foreign)


def test_idor_foreign_thesis_url(
    signed_in: Client, student: Membership, foreign: dict[str, Any]
) -> None:
    """Foreign thesis in the URL: 403 (no membership there), nothing changes."""
    with _now():
        for name, ids, data in _idor_targets(foreign):
            response = signed_in.post(_u(name, foreign["thesis"].pk, **ids), data)
            assert response.status_code == 403, name
        assert signed_in.get(_u("team", foreign["thesis"].pk)).status_code == 403
        assert (
            signed_in.post(
                _u("team_invite", foreign["thesis"].pk),
                {"email": "x@example.org", "role": Role.DIRECTOR, "new_organization": "X"},
            ).status_code
            == 403
        )
    _foreign_unchanged(foreign)


# --------------------------------------------------------------------------- 2. escalation


def test_nobody_becomes_phd_student_through_change_role(
    signed_in: Client, student: Membership
) -> None:
    director = MembershipFactory.create(thesis=student.thesis, role=Role.DIRECTOR)
    with _now():
        signed_in.post(
            _u("team_change_role", student.thesis.pk, membership_id=director.pk),
            {"role": Role.PHD_STUDENT},
        )
    assert list(
        Membership.objects.active().filter(thesis=student.thesis, role=Role.PHD_STUDENT)
    ) == [student]
    director.refresh_from_db()
    assert director.active_until is None
    with pytest.raises(TeamError):  # and not through the service either
        services.change_role(director, Role.PHD_STUDENT, by=student.user)


def test_supervisor_cannot_change_their_own_role(client: Client, student: Membership) -> None:
    director = MembershipFactory.create(
        thesis=student.thesis, role=Role.DIRECTOR, user__password=PASSWORD
    )
    with time_machine.travel(T0):
        mfa_login(client, director.user, PASSWORD)
    with _now():
        response = client.post(
            _u("team_change_role", student.thesis.pk, membership_id=director.pk),
            {"role": Role.CO_SUPERVISOR},
        )
    assert response.status_code == 403


def test_phd_student_cannot_be_removed(signed_in: Client, student: Membership) -> None:
    with _now():
        signed_in.post(_u("team_remove", student.thesis.pk, membership_id=student.pk))
    assert Membership.objects.active().filter(pk=student.pk).exists()


def test_phd_student_role_cannot_be_changed_away(signed_in: Client, student: Membership) -> None:
    """Otherwise the thesis would lose its super-role (and a second one could be invited)."""
    with _now():
        signed_in.post(
            _u("team_change_role", student.thesis.pk, membership_id=student.pk),
            {"role": Role.DIRECTOR},
        )
    assert Membership.objects.active().filter(pk=student.pk, role=Role.PHD_STUDENT).exists()


def test_supervisor_cannot_be_invited_as_csi(signed_in: Client, student: Membership) -> None:
    director = MembershipFactory.create(thesis=student.thesis, role=Role.DIRECTOR)
    with _now():
        response = signed_in.post(
            _u("team_invite", student.thesis.pk),
            {
                "email": director.user.email,
                "role": Role.CSI_MEMBER,
                "organization": str(director.organization.pk),
            },
        )
        html = signed_in.get(_u("team", student.thesis.pk)).content.decode()
    assert response.status_code == 302
    assert not Invitation.objects.filter(email=director.user.email).exists()
    assert "Un membre du CSI ne peut pas" in html


def test_csi_member_cannot_also_be_made_supervisor(signed_in: Client, student: Membership) -> None:
    csi = MembershipFactory.create(
        thesis=student.thesis,
        role=Role.CSI_MEMBER,
        active_from=T0 - dt.timedelta(days=1),
        active_until=T0 + dt.timedelta(days=30),
    )
    with _now():
        signed_in.post(
            _u("team_invite", student.thesis.pk),
            {
                "email": csi.user.email,
                "role": Role.DIRECTOR,
                "organization": str(csi.organization.pk),
            },
        )
    assert not Invitation.objects.filter(email=csi.user.email).exists()


# --------------------------------------------------------------------------- 3. session fixation


def _fixed_session(client: Client) -> str:
    """An attacker-planted session: exists before any authentication."""
    session = client.session
    session["planted"] = True
    session.save()
    client.cookies["sessionid"] = session.session_key  # type: ignore[assignment]
    return str(session.session_key)


def test_session_key_rotates_at_sign_in_and_after_second_factor(student: Membership) -> None:
    client = Client()
    planted = _fixed_session(client)
    password_login(client, student.user, PASSWORD)
    after_password = client.session.session_key
    client.post(reverse("mfa_authenticate"), {"code": totp_code(student.user.totp_secret)})  # type: ignore[attr-defined]
    after_mfa = client.session.session_key
    assert client.session.get("_auth_user_id") == str(student.user.pk)
    assert after_mfa not in (planted, after_password)
    # The planted key gives nothing: it no longer maps to an authenticated session.
    attacker = Client()
    attacker.cookies["sessionid"] = planted
    assert attacker.get(_u("dashboard", student.thesis.pk)).status_code == 302


def test_session_key_rotates_at_invitation_acceptance(
    student: Membership, django_capture_on_commit_callbacks: Any
) -> None:
    mail.outbox.clear()
    with django_capture_on_commit_callbacks(execute=True):
        services.create_invitation(
            thesis=student.thesis,
            email="fixation@example.org",
            role=Role.DIRECTOR,
            organization=student.organization,
            by=student.user,
        )
    token = re.search(r"/invitation/([A-Za-z0-9_-]{43})/", str(mail.outbox[0].body)).group(1)  # type: ignore[union-attr]
    client = Client()
    planted = _fixed_session(client)
    client.post(reverse("invitation_continue"), {"token": token})
    before_accept = client.session.session_key
    client.post(
        reverse("invitation_accept"),
        {"first_name": "P", "last_name": "N", "password1": PASSWORD, "password2": PASSWORD},
    )
    assert client.session.get("_auth_user_id")
    assert client.session.session_key not in (planted, before_accept)


# --------------------------------------------------------------------------- 4. open redirect

EVIL = ["https://evil.example/", "//evil.example/", "http://evil.example", "/\\evil.example"]


@pytest.mark.parametrize("target", EVIL)
def test_no_open_redirect_after_sign_in(student: Membership, target: str) -> None:
    client = Client()
    client.post(
        reverse("account_login") + f"?next={target}",
        {"login": student.user.email, "password": PASSWORD, "next": target},
    )
    response = client.post(
        reverse("mfa_authenticate"),
        {"code": totp_code(student.user.totp_secret)},  # type: ignore[attr-defined]
    )
    assert response.status_code == 302
    assert "evil.example" not in response["Location"]


@pytest.mark.parametrize("target", EVIL)
def test_no_open_redirect_after_reauthentication(
    signed_in: Client, student: Membership, target: str
) -> None:
    with _now(30):
        response = signed_in.post(
            reverse("mfa_reauthenticate") + f"?next={target}",
            {"code": totp_code(student.user.totp_secret), "next": target},  # type: ignore[attr-defined]
        )
    assert response.status_code == 302
    assert "evil.example" not in response["Location"]


@pytest.mark.parametrize("target", EVIL)
def test_no_open_redirect_after_password_reauthentication(signed_in: Client, target: str) -> None:
    with _now(30):
        response = signed_in.post(
            reverse("account_reauthenticate") + f"?next={target}",
            {"password": PASSWORD, "next": target},
        )
    assert response.status_code == 302
    assert "evil.example" not in response["Location"]


# --------------------------------------------------------------------------- 5. CSRF

_SAMPLES = {"uuid": SAMPLE_UUID, "str": "sample", "int": "1", "slug": "sample", "path": "sample"}
# Regex routes cannot be filled generically: concrete samples.
_REGEX_SAMPLES = {
    "account_reset_password_from_key": "/accounts/password/reset/key/1-sample/",
    "account_confirm_email": "/accounts/confirm-email/sample/",
}


def _post_routes() -> list[str]:
    paths: list[str] = []

    def walk(patterns: list[Any], prefix: str) -> None:
        for p in patterns:
            route = prefix + str(p.pattern)
            if isinstance(p, URLResolver):
                walk(p.url_patterns, route)
            elif isinstance(p, URLPattern) and not route.startswith("admin/"):
                if route.startswith("^") or "(?P<" in route:
                    if p.name in _REGEX_SAMPLES:
                        paths.append(_REGEX_SAMPLES[p.name])
                    continue
                filled = re.sub(r"<(?:(\w+):)?\w+>", lambda m: _SAMPLES[m.group(1) or "str"], route)
                paths.append("/" + filled)

    walk(get_resolver().url_patterns, "")
    return sorted(set(paths))


def test_route_walk_finds_the_sensitive_post_routes() -> None:
    routes = _post_routes()
    for expected in (
        "/accounts/login/",
        "/accounts/2fa/authenticate/",
        "/accounts/2fa/reauthenticate/",
        "/invitation/",
        "/invitation/accept/",
        f"/t/{SAMPLE_UUID}/team/invite/",
        f"/t/{SAMPLE_UUID}/team/{SAMPLE_UUID}/remove/",
    ):
        assert expected in routes


@pytest.mark.parametrize("path", _post_routes())
def test_post_without_csrf_token_is_refused(path: str) -> None:
    client = Client(enforce_csrf_checks=True)
    client.force_login(UserFactory.create())
    response = client.post(path, {"x": "1"})
    assert response.status_code == 403, path
    assert b"CSRF" in response.content, path


def test_no_view_is_csrf_exempt() -> None:
    exempt = []

    def walk(patterns: list[Any], prefix: str) -> None:
        for p in patterns:
            if isinstance(p, URLResolver):
                walk(p.url_patterns, prefix + str(p.pattern))
            elif getattr(p.callback, "csrf_exempt", False):
                exempt.append(prefix + str(p.pattern))

    walk(get_resolver().url_patterns, "")
    assert exempt == []


# --------------------------------------------------------------------------- 6. TOTP rate limiting


def test_totp_step_is_rate_limited(student: Membership) -> None:
    """After 5 wrong codes, even the right code is refused at the TOTP step."""
    client = Client(REMOTE_ADDR="192.0.2.80")
    with time_machine.travel(T0 + dt.timedelta(seconds=5), tick=False):
        password_login(client, student.user, PASSWORD)
        good = totp_code(student.user.totp_secret)  # type: ignore[attr-defined]
        wrong = f"{(int(good) + 1) % 1_000_000:06d}"
        for _ in range(5):
            client.post(reverse("mfa_authenticate"), {"code": wrong})
        response = client.post(reverse("mfa_authenticate"), {"code": good})
    assert "_auth_user_id" not in client.session
    assert response.status_code == 200
    assert response.context["form"].errors


def test_totp_limit_follows_the_user_not_the_session(student: Membership) -> None:
    """Spreading guesses over fresh sessions (or IPs) does not reset the counter."""
    with time_machine.travel(T0 + dt.timedelta(seconds=5), tick=False):
        good = totp_code(student.user.totp_secret)  # type: ignore[attr-defined]
        wrong = f"{(int(good) + 1) % 1_000_000:06d}"
        for i in range(5):
            c = Client(REMOTE_ADDR=f"192.0.2.{100 + i}")
            password_login(c, student.user, PASSWORD)
            c.post(reverse("mfa_authenticate"), {"code": wrong})
        last = Client(REMOTE_ADDR="192.0.2.200")
        password_login(last, student.user, PASSWORD)
        last.post(reverse("mfa_authenticate"), {"code": good})
    assert "_auth_user_id" not in last.session


def test_reauthentication_step_is_rate_limited_too(signed_in: Client, student: Membership) -> None:
    with time_machine.travel(T0 + dt.timedelta(minutes=30, seconds=5), tick=False):
        good = totp_code(student.user.totp_secret)  # type: ignore[attr-defined]
        wrong = f"{(int(good) + 1) % 1_000_000:06d}"
        for _ in range(5):
            signed_in.post(reverse("mfa_reauthenticate"), {"code": wrong})
        response = signed_in.post(reverse("mfa_reauthenticate"), {"code": good})
    assert response.status_code == 200
    assert response.context["form"].errors


# --------------------------------------------------------------------------- 7. header injection

INJECTED = [
    "victim@example.org\r\nBcc: attacker@example.org",
    "victim@example.org\nBcc: attacker@example.org",
    "victim@example.org\rSubject: phishing",
]


@pytest.mark.parametrize("address", INJECTED)
def test_invite_form_rejects_crlf_address(
    signed_in: Client, student: Membership, address: str
) -> None:
    with _now():
        response = signed_in.post(
            _u("team_invite", student.thesis.pk),
            {"email": address, "role": Role.DIRECTOR, "organization": str(student.organization.pk)},
        )
    assert response.status_code == 400
    assert not Invitation.objects.exists()


@pytest.mark.parametrize("address", INJECTED)
def test_service_rejects_crlf_address(
    student: Membership, address: str, django_capture_on_commit_callbacks: Any
) -> None:
    """Defence in depth: callers other than the form (command, future modules)."""
    mail.outbox.clear()
    with pytest.raises(TeamError), django_capture_on_commit_callbacks(execute=True):
        services.create_invitation(
            thesis=student.thesis,
            email=address,
            role=Role.DIRECTOR,
            organization=student.organization,
            by=student.user,
        )
    assert not Invitation.objects.exists()
    assert mail.outbox == []


def test_create_thesis_command_rejects_crlf_address() -> None:
    import io

    from django.core.management import CommandError, call_command

    with pytest.raises(CommandError):
        call_command(
            "create_thesis",
            "--title=Thèse fictive",
            "--cifre-start=2026-03-01",
            "--first-enrollment=2026-03-15",
            "--planned-end=2029-02-28",
            "--student-email=student@example.org\r\nBcc: attacker@example.org",
            stdout=io.StringIO(),
        )
    assert mail.outbox == []


# --------------------------------------------------------------------------- 8. deactivated account


def test_deactivated_account_loses_its_open_session(signed_in: Client, student: Membership) -> None:
    with _now():
        assert signed_in.get(_u("dashboard", student.thesis.pk)).status_code == 200
        User.objects.filter(pk=student.user.pk).update(is_active=False)
        page = signed_in.get(_u("dashboard", student.thesis.pk))
        action = signed_in.post(_u("team_invite", student.thesis.pk), {"email": "x@example.org"})
    assert page.status_code == 302
    assert page["Location"].startswith(reverse("account_login"))
    assert action.status_code == 302
    assert not Invitation.objects.exists()


def test_deactivated_account_cannot_sign_in_again(student: Membership) -> None:
    User.objects.filter(pk=student.user.pk).update(is_active=False)
    client = Client()
    response = client.post(
        reverse("account_login"), {"login": student.user.email, "password": PASSWORD}
    )
    assert "_auth_user_id" not in client.session
    assert response.get("Location") != reverse("mfa_authenticate")


def test_deactivated_member_has_no_rights_even_with_active_membership(student: Membership) -> None:
    from apps.accounts.permissions import Action, can

    User.objects.filter(pk=student.user.pk).update(is_active=False)
    student.user.refresh_from_db()
    assert Membership.objects.filter(pk=student.pk, active_until__isnull=True).exists()
    assert not can(student.user, Action.VIEW_THESIS, student.thesis)


def test_task_treats_a_header_error_as_a_failed_send(student: Membership) -> None:
    """Last line: a bad address reaching the task (e.g. an old row) fails cleanly."""
    from apps.accounts.tasks import send_invitation_email

    inv = Invitation.objects.create(
        thesis=student.thesis,
        email="victim@example.org\r\nBcc: attacker@example.org",
        role=Role.DIRECTOR,
        organization=student.organization,
        invited_by=student.user,
    )
    mail.outbox.clear()
    send_invitation_email(str(inv.pk))
    inv.refresh_from_db()
    assert inv.email_status == Invitation.EmailStatus.FAILED
    assert inv.token_hash is None
    assert mail.outbox == []
