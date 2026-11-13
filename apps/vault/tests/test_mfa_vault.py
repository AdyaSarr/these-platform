"""MFA secrets in the vault: deploy check, sealing of clear secrets, and the real
allauth flows end to end (transaction=True: commits as in production)."""

import datetime as dt
import secrets
from io import StringIO
from typing import Any

import pytest
from allauth.mfa.models import Authenticator
from allauth.mfa.recovery_codes.internal.auth import RecoveryCodes
from allauth.mfa.totp.internal.auth import SECRET_SESSION_KEY
from django.core.management import call_command
from django.test import Client
from django.urls import reverse

from apps.accounts.tests.factories import UserFactory, totp_code
from apps.accounts.tests.helpers import mfa_login, mfa_reauthenticate
from apps.vault import checks, services
from apps.vault.models import AuditEvent

PASSWORD = secrets.token_urlsafe(16)
T0 = dt.datetime(2026, 12, 1, 10, 0, tzinfo=dt.UTC)


# --------------------------------------------------------------------------- deploy check


@pytest.mark.django_db
def test_deploy_check_passes_with_the_database() -> None:
    assert checks.mfa_secret_round_trip(None, databases=["default"]) == []


def test_deploy_check_needs_the_database_option() -> None:
    """Database checks run only with `check --database default` (Django convention)."""
    assert checks.mfa_secret_round_trip(None, databases=None) == []


@pytest.mark.django_db
@pytest.mark.without_vault_keys
def test_deploy_check_fails_without_vault_init() -> None:
    (error,) = checks.mfa_secret_round_trip(None, databases=["default"])
    assert error.id == "vault.E001"
    assert "vault_init" in (error.hint or "")


@pytest.mark.django_db
def test_deploy_check_fails_on_a_broken_round_trip(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(services, "decrypt_secret", lambda text: "something else")
    (error,) = checks.mfa_secret_round_trip(None, databases=["default"])
    assert error.id == "vault.E001"


@pytest.mark.django_db
def test_check_command_runs_it_with_deploy_and_database() -> None:
    out = StringIO()
    call_command("check", "--deploy", "--database", "default", "--tag", "database", stdout=out)
    assert "no issues" in out.getvalue()


# --------------------------------------------------------------------------- sealing clear secrets


def _clear_authenticators() -> tuple[Any, Any]:
    user = UserFactory.create(mfa=False)
    totp = Authenticator.objects.create(
        user=user,
        type=Authenticator.Type.TOTP,
        data={"secret": "JBSWY3DPEHPK3PXP"},  # pragma: allowlist secret
    )
    codes = Authenticator.objects.create(
        user=user,
        type=Authenticator.Type.RECOVERY_CODES,
        data={"seed": "0123456789abcdef", "used_mask": 3},  # pragma: allowlist secret
    )
    return totp, codes


@pytest.mark.django_db(transaction=True)
def test_clear_mfa_secrets_are_sealed_once() -> None:
    totp, codes = _clear_authenticators()
    already = services.encrypt_secret("already sealed")
    other = Authenticator.objects.create(
        user=UserFactory.create(mfa=False), type=Authenticator.Type.TOTP, data={"secret": already}
    )
    out = StringIO()
    call_command("vault_encrypt_mfa_secrets", stdout=out)
    assert "2 secret(s) sealed" in out.getvalue()
    totp.refresh_from_db()
    codes.refresh_from_db()
    other.refresh_from_db()
    assert services.decrypt_secret(totp.data["secret"]) == "JBSWY3DPEHPK3PXP"
    assert services.decrypt_secret(codes.data["seed"]) == "0123456789abcdef"
    assert codes.data["used_mask"] == 3
    assert other.data["secret"] == already  # untouched
    assert AuditEvent.objects.filter(action="MFA_SECRETS_SEALED").get().details_json == {"rows": 2}
    out = StringIO()
    call_command("vault_encrypt_mfa_secrets", stdout=out)  # idempotent
    assert "0 secret(s) sealed" in out.getvalue()
    assert AuditEvent.objects.filter(action="MFA_SECRETS_SEALED").count() == 1


@pytest.mark.django_db(transaction=True)
def test_sealed_totp_still_signs_in(client: Client, time_machine: Any) -> None:
    time_machine.move_to(T0, tick=False)
    user = UserFactory.create(password=PASSWORD, mfa=False)
    fictitious = "JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP"  # pragma: allowlist secret
    Authenticator.objects.create(
        user=user, type=Authenticator.Type.TOTP, data={"secret": fictitious}
    )
    call_command("vault_encrypt_mfa_secrets", stdout=StringIO())
    user.totp_secret = fictitious  # type: ignore[attr-defined]
    mfa_login(client, user, PASSWORD)


# --------------------------------------------------------------------------- allauth end to end


@pytest.mark.django_db(transaction=True)
def test_real_allauth_flows_with_vault_sealed_secrets(time_machine: Any) -> None:
    """Sign-in, TOTP activation (recovery codes generated with it), sign-in with a
    recovery code, regeneration, re-authentication: no LockOrderError, every secret
    sealed, every step audited. Steps 31 s apart (TOTP anti-replay)."""
    now = T0
    time_machine.move_to(now, tick=False)

    def later() -> None:
        nonlocal now
        now += dt.timedelta(seconds=31)
        time_machine.move_to(now, tick=False)

    user = UserFactory.create(password=PASSWORD, mfa=False)
    client = Client()
    # 1. Password sign-in of an account without factor, then TOTP activation.
    client.post(reverse("account_login"), {"login": user.email, "password": PASSWORD})
    client.get(reverse("mfa_activate_totp"))
    secret = client.session.get(SECRET_SESSION_KEY)
    if secret is None:  # allauth asked for a re-authentication first
        client.post(reverse("account_reauthenticate"), {"password": PASSWORD})
        client.get(reverse("mfa_activate_totp"))
        secret = client.session[SECRET_SESSION_KEY]
    response = client.post(reverse("mfa_activate_totp"), {"code": totp_code(secret)})
    assert response.status_code == 302, response.content[:300]
    totp = Authenticator.objects.get(user=user, type=Authenticator.Type.TOTP)
    codes = Authenticator.objects.get(user=user, type=Authenticator.Type.RECOVERY_CODES)
    assert totp.data["secret"].startswith("vault1:")
    assert codes.data["seed"].startswith("vault1:")
    assert secret not in str(list(Authenticator.objects.values("data")))
    user.totp_secret = secret  # type: ignore[attr-defined]

    # 2. Full sign-in with the TOTP code.
    later()
    signed_in = Client()
    mfa_login(signed_in, user, PASSWORD)

    # 3. Sign-in with a recovery code (decrypts the seed, marks the code used).
    later()
    code = RecoveryCodes(codes).get_unused_codes()[0]
    with_code = Client()
    with_code.post(reverse("account_login"), {"login": user.email, "password": PASSWORD})
    with_code.post(reverse("mfa_authenticate"), {"code": code})
    assert with_code.session.get("_auth_user_id") == str(user.pk)

    # 4. Re-authentication with the factor, then regeneration of the codes.
    later()
    mfa_reauthenticate(signed_in, user)
    old_seed = Authenticator.objects.get(pk=codes.pk).data["seed"]
    assert signed_in.post(reverse("mfa_generate_recovery_codes")).status_code == 302
    regenerated = Authenticator.objects.get(user=user, type=Authenticator.Type.RECOVERY_CODES)
    assert regenerated.data["seed"].startswith("vault1:")
    assert regenerated.data["seed"] != old_seed

    actions = set(AuditEvent.objects.filter(actor=user).values_list("action", flat=True))
    assert {"LOGIN_SUCCEEDED", "MFA_FACTOR_ADDED", "MFA_FACTOR_USED", "MFA_FACTOR_RESET"} <= actions


# --------------------------------------------------------------------------- vault.E002


@pytest.mark.django_db
def test_every_domain_has_a_kek() -> None:
    assert checks.every_domain_has_a_kek(None, databases=["default"]) == []
    assert checks.every_domain_has_a_kek(None, databases=None) == []


@pytest.mark.django_db
@pytest.mark.without_vault_keys
def test_missing_kek_domains_are_reported() -> None:
    from apps.vault.models import KeyEncryptionKey

    KeyEncryptionKey.objects.create(provider="env", domain="PUBLIC", version=1)
    (error,) = checks.every_domain_has_a_kek(None, databases=["default"])
    assert error.id == "vault.E002"
    assert "vault_init" in (error.hint or "")
    assert "CONFIDENTIAL" in error.msg
    assert "PUBLIC" not in error.msg
