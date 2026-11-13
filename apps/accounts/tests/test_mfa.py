"""Mandatory second factor (acceptance criterion), sign-in stages, TOTP replay,
recovery codes, and production refusing clear MFA secrets."""

import datetime as dt
import secrets
from typing import Any
from unittest import mock

import pytest
import time_machine
from allauth.mfa.models import Authenticator
from allauth.mfa.recovery_codes.internal.auth import RecoveryCodes
from django.test import Client
from django.urls import reverse

from apps.accounts.models import Membership
from apps.accounts.roles import Role
from apps.accounts.tests.factories import MembershipFactory, UserFactory, totp_code
from apps.accounts.tests.helpers import (
    mfa_login,
    mfa_reauthenticate,
    password_login,
    password_reauthenticate,
)
from tests.prod_helpers import prod_env, run_python
from tests.test_thesis_routes import _path, thesis_routes

pytestmark = pytest.mark.django_db

PASSWORD = secrets.token_urlsafe(16)  # generated: no password literal in the repo


@pytest.fixture
def student() -> Membership:
    m = MembershipFactory.create(role=Role.PHD_STUDENT, user__password=PASSWORD)
    m.user.refresh_from_db()
    return m


def _without_mfa(role: Role = Role.DIRECTOR) -> Membership:
    return MembershipFactory.create(role=role, user__mfa=False, user__password=PASSWORD)


# --------------------------------------------------------------------------- enforcement


def test_dashboard_unreachable_without_second_factor(client: Client) -> None:
    """Acceptance: no dashboard without an enrolled second factor."""
    m = _without_mfa()
    client.force_login(m.user)
    response = client.get(reverse("accounts:dashboard", kwargs={"thesis_id": m.thesis.pk}))
    assert response.status_code == 302
    assert response["Location"] == reverse("mfa_index")


def test_every_thesis_route_requires_a_second_factor(client: Client) -> None:
    m = _without_mfa()
    client.force_login(m.user)
    for route, _ in thesis_routes():
        path = _path(route, m.thesis.pk)
        for method in (client.get, client.post):
            response = method(path)
            assert response.status_code == 302, (path, response.status_code)
            assert response["Location"] == reverse("mfa_index")


def test_home_requires_a_second_factor(client: Client) -> None:
    client.force_login(UserFactory.create(mfa=False))
    assert client.get(reverse("core:home"))["Location"] == reverse("mfa_index")


def test_htmx_request_gets_a_full_page_redirect(client: Client) -> None:
    m = _without_mfa()
    client.force_login(m.user)
    response = client.get(
        reverse("accounts:team", kwargs={"thesis_id": m.thesis.pk}), headers={"HX-Request": "true"}
    )
    assert response["HX-Redirect"] == reverse("mfa_index")


def test_enrollment_pages_stay_reachable(client: Client) -> None:
    client.force_login(UserFactory.create(mfa=False))
    assert client.get(reverse("mfa_index")).status_code == 200
    assert client.post(reverse("logout")).status_code == 302


def test_recovery_codes_alone_do_not_count(client: Client) -> None:
    user = UserFactory.create(mfa=False)
    RecoveryCodes.activate(user)
    client.force_login(user)
    assert client.get(reverse("core:home"))["Location"] == reverse("mfa_index")


def test_enrolled_user_passes(client: Client) -> None:
    m = MembershipFactory.create(role=Role.DIRECTOR)
    client.force_login(m.user)
    assert (
        client.get(reverse("accounts:team", kwargs={"thesis_id": m.thesis.pk})).status_code == 200
    )


# --------------------------------------------------------------------------- sign-in stages


def test_password_alone_does_not_sign_in_an_enrolled_user(
    client: Client, student: Membership
) -> None:
    password_login(client, student.user, PASSWORD)
    assert "_auth_user_id" not in client.session
    response = client.get(reverse("accounts:dashboard", kwargs={"thesis_id": student.thesis.pk}))
    assert response.status_code == 302
    assert response["Location"].startswith(reverse("account_login"))


def test_password_then_totp_signs_in(client: Client, student: Membership) -> None:
    mfa_login(client, student.user, PASSWORD)
    url = reverse("accounts:dashboard", kwargs={"thesis_id": student.thesis.pk})
    assert client.get(url).status_code == 200


def test_wrong_totp_code_is_refused(client: Client, student: Membership) -> None:
    password_login(client, student.user, PASSWORD)
    good = totp_code(student.user.totp_secret)  # type: ignore[attr-defined]
    wrong = f"{(int(good) + 1) % 1_000_000:06d}"
    client.post(reverse("mfa_authenticate"), {"code": wrong})
    assert "_auth_user_id" not in client.session


def test_totp_code_cannot_be_replayed(student: Membership) -> None:
    """The same code used twice (e.g. shoulder-surfed) is refused the second time."""
    with time_machine.travel(dt.datetime(2026, 12, 1, 10, 0, 5, tzinfo=dt.UTC), tick=False):
        first, second = Client(), Client()
        code = totp_code(student.user.totp_secret)  # type: ignore[attr-defined]
        password_login(first, student.user, PASSWORD)
        first.post(reverse("mfa_authenticate"), {"code": code})
        assert first.session.get("_auth_user_id") == str(student.user.pk)
        password_login(second, student.user, PASSWORD)
        second.post(reverse("mfa_authenticate"), {"code": code})
        assert "_auth_user_id" not in second.session


def test_recovery_code_signs_in_once(student: Membership) -> None:
    rc = RecoveryCodes.activate(student.user)
    code = rc.get_unused_codes()[0]
    first, second = Client(), Client()
    password_login(first, student.user, PASSWORD)
    first.post(reverse("mfa_authenticate"), {"code": code})
    assert first.session.get("_auth_user_id") == str(student.user.pk)
    password_login(second, student.user, PASSWORD)
    second.post(reverse("mfa_authenticate"), {"code": code})
    assert "_auth_user_id" not in second.session


def test_passkey_login_and_browser_trust_are_disabled() -> None:
    from django.urls import NoReverseMatch

    for name in ("mfa_login_webauthn", "mfa_signup_webauthn", "mfa_trust"):
        with pytest.raises(NoReverseMatch):
            reverse(name)


# --------------------------------------------------------------------------- MFA secrets at rest


def test_mfa_secrets_go_through_the_adapter() -> None:
    """TOTP key and recovery-code seed are stored through MFAAdapter.encrypt (vault in M2)."""
    from apps.accounts.adapters import MFAAdapter

    with mock.patch.object(MFAAdapter, "encrypt", side_effect=lambda t: f"enc:{t}") as enc:
        user = UserFactory.create()
        RecoveryCodes.activate(user)
    assert enc.call_count >= 2
    for a in Authenticator.objects.filter(user=user):
        stored = a.data.get("secret") or a.data.get("seed")
        assert stored.startswith("enc:"), a.type


@pytest.mark.parametrize("module", ["config.wsgi", "config.asgi"])
def test_prod_serves_with_the_vault_adapter(module: str) -> None:
    """The guard is structural: it needs no database (prod tests have none)."""
    result = run_python(f"import {module}\nprint('ok')", prod_env())
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().endswith("ok")


@pytest.mark.parametrize(
    "adapter",
    [
        "tests.mfa_fake_adapter.IdentityMFAAdapter",
        "tests.mfa_fake_adapter.ReversibleMFAAdapter",  # reversible: a behaviour check passes
        "tests.mfa_fake_adapter.OverridingMFAAdapter",  # right class, vault bypassed
        "allauth.mfa.adapter.DefaultMFAAdapter",
    ],
)
def test_prod_refuses_any_adapter_but_the_vault_one(adapter: str) -> None:
    code = (
        "import django, os\n"
        "os.environ['DJANGO_SETTINGS_MODULE'] = 'config.settings.prod'\n"
        "django.setup()\n"
        "from django.conf import settings\n"
        f"settings.MFA_ADAPTER = {adapter!r}\n"
        "from apps.accounts.guards import require_mfa_encryption\n"
        "require_mfa_encryption()\n"
    )
    result = run_python(code, prod_env())
    assert result.returncode != 0
    assert "MFAAdapter" in result.stderr


def test_guard_refuses_an_adapter_that_stops_delegating_to_the_vault(
    settings: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from django.core.exceptions import ImproperlyConfigured

    from apps.accounts.adapters import MFAAdapter
    from apps.accounts.guards import require_mfa_encryption

    settings.MFA_ENCRYPTION_REQUIRED = True
    require_mfa_encryption()  # the real one passes, without the database
    monkeypatch.setattr(MFAAdapter, "encrypt", lambda self, text: text)
    with pytest.raises(ImproperlyConfigured):
        require_mfa_encryption()


@pytest.mark.django_db
def test_totp_secret_and_recovery_seed_are_not_readable_in_the_database() -> None:
    from apps.vault import services

    user = UserFactory.create()  # enrolls a TOTP through allauth
    RecoveryCodes.activate(user)
    rows = list(Authenticator.objects.filter(user=user).values("type", "data"))
    dump = str(rows)
    assert user.totp_secret not in dump  # type: ignore[attr-defined]
    for row in rows:
        stored = row["data"].get("secret") or row["data"].get("seed")
        assert stored.startswith("vault1:"), row["type"]
    totp = next(r for r in rows if r["type"] == Authenticator.Type.TOTP)
    assert services.decrypt_secret(totp["data"]["secret"]) == user.totp_secret  # type: ignore[attr-defined]


def test_guard_is_inactive_outside_production(settings: Any) -> None:
    from apps.accounts.guards import require_mfa_encryption

    settings.MFA_ENCRYPTION_REQUIRED = False
    require_mfa_encryption()  # does not raise in dev and tests


# ------------------------------------------------------------------- recovery codes: MFA re-auth


RC_ROUTES = [
    "mfa_view_recovery_codes",
    "mfa_download_recovery_codes",
    "mfa_generate_recovery_codes",
]


@pytest.mark.parametrize("name", RC_ROUTES)
def test_recovery_codes_need_recent_mfa(client: Client, student: Membership, name: str) -> None:
    """Condition (a): showing or regenerating codes requires a recent second factor."""
    RecoveryCodes.activate(student.user)
    client.force_login(student.user)  # signed in, but no recent factor recorded
    response = client.get(reverse(name))
    assert response.status_code == 302
    assert response["Location"].startswith(reverse("mfa_reauthenticate"))


@pytest.mark.parametrize("name", RC_ROUTES)
def test_password_reauth_is_not_enough_for_recovery_codes(
    client: Client, student: Membership, name: str
) -> None:
    RecoveryCodes.activate(student.user)
    with time_machine.travel(dt.datetime(2026, 12, 1, 10, 0, tzinfo=dt.UTC)):
        mfa_login(client, student.user, PASSWORD)
    with time_machine.travel(dt.datetime(2026, 12, 1, 10, 20, tzinfo=dt.UTC)):
        password_reauthenticate(client, PASSWORD)
        response = client.get(reverse(name))
    assert response.status_code == 302
    assert response["Location"].startswith(reverse("mfa_reauthenticate"))


def test_recovery_codes_shown_after_mfa_reauth(client: Client, student: Membership) -> None:
    rc = RecoveryCodes.activate(student.user)
    with time_machine.travel(dt.datetime(2026, 12, 1, 10, 0, tzinfo=dt.UTC)):
        mfa_login(client, student.user, PASSWORD)
    with time_machine.travel(dt.datetime(2026, 12, 1, 10, 30, 1, tzinfo=dt.UTC)):
        mfa_reauthenticate(client, student.user)
        response = client.get(reverse("mfa_view_recovery_codes"))
    assert response.status_code == 200
    assert rc.get_unused_codes()[0] in response.content.decode()


def test_recovery_codes_page_expires_after_10_minutes(client: Client, student: Membership) -> None:
    RecoveryCodes.activate(student.user)
    with time_machine.travel(dt.datetime(2026, 12, 1, 10, 0, tzinfo=dt.UTC)):
        mfa_login(client, student.user, PASSWORD)
    with time_machine.travel(dt.datetime(2026, 12, 1, 10, 10, 1, tzinfo=dt.UTC)):
        response = client.get(reverse("mfa_view_recovery_codes"))
    assert response["Location"].startswith(reverse("mfa_reauthenticate"))


# --------------------------------------------------------------------------- admin (dev/test only)


def test_admin_login_goes_through_allauth(client: Client) -> None:
    response = client.get("/admin/login/")
    assert response.status_code == 302
    assert response["Location"].startswith(reverse("account_login"))


def test_login_code_cannot_be_reused_for_reauthentication(
    client: Client, student: Membership
) -> None:
    """Anti-replay also covers re-authentication: the next 30-second code is needed."""
    with time_machine.travel(dt.datetime(2026, 12, 1, 10, 0, 5, tzinfo=dt.UTC), tick=False):
        mfa_login(client, student.user, PASSWORD)
        code = totp_code(student.user.totp_secret)  # type: ignore[attr-defined]
        response = client.post(reverse("mfa_reauthenticate"), {"code": code})
    assert response.status_code == 200  # form shown again with an error
    assert response.context["form"].errors


# ------------------------------------------------------------------- recovery codes: shown once


def _rc_signed_in(client: Client, student: Membership) -> None:
    """Signed in at T0 and re-authenticated with the factor at T0 + 1 min."""
    with time_machine.travel(dt.datetime(2026, 12, 1, 10, 0, tzinfo=dt.UTC)):
        mfa_login(client, student.user, PASSWORD)
    with time_machine.travel(dt.datetime(2026, 12, 1, 10, 1, tzinfo=dt.UTC)):
        mfa_reauthenticate(client, student.user)


def test_recovery_codes_are_shown_only_once(client: Client, student: Membership) -> None:
    rc = RecoveryCodes.activate(student.user)
    codes = rc.get_unused_codes()
    _rc_signed_in(client, student)
    with time_machine.travel(dt.datetime(2026, 12, 1, 10, 2, tzinfo=dt.UTC)):
        first = client.get(reverse("mfa_view_recovery_codes")).content.decode()
        second = client.get(reverse("mfa_view_recovery_codes")).content.decode()
        download = client.get(reverse("mfa_download_recovery_codes"))
    assert all(code in first for code in codes)
    assert not any(code in second for code in codes)
    assert reverse("mfa_generate_recovery_codes") in second  # only option left: regenerate
    assert download.status_code == 403


def test_regeneration_needs_the_second_factor(client: Client, student: Membership) -> None:
    RecoveryCodes.activate(student.user)
    with time_machine.travel(dt.datetime(2026, 12, 1, 10, 0, tzinfo=dt.UTC)):
        mfa_login(client, student.user, PASSWORD)
    with time_machine.travel(dt.datetime(2026, 12, 1, 10, 30, tzinfo=dt.UTC)):
        password_reauthenticate(client, PASSWORD)
        refused = client.post(reverse("mfa_generate_recovery_codes"))
    assert refused["Location"].startswith(reverse("mfa_reauthenticate"))


def test_regenerated_codes_replace_the_old_ones_and_show_once(
    client: Client, student: Membership
) -> None:
    old = RecoveryCodes.activate(student.user).get_unused_codes()
    _rc_signed_in(client, student)
    with time_machine.travel(dt.datetime(2026, 12, 1, 10, 2, tzinfo=dt.UTC)):
        client.get(reverse("mfa_view_recovery_codes"))  # old codes seen once
        assert client.post(reverse("mfa_generate_recovery_codes")).status_code == 302
        new = RecoveryCodes(
            Authenticator.objects.get(user=student.user, type=Authenticator.Type.RECOVERY_CODES)
        ).get_unused_codes()
        shown = client.get(reverse("mfa_view_recovery_codes")).content.decode()
        again = client.get(reverse("mfa_view_recovery_codes")).content.decode()
    assert set(new).isdisjoint(old)
    assert all(code in shown for code in new)
    assert not any(code in again for code in new)
