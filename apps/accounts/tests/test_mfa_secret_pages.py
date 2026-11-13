"""Pages that show an MFA secret: never cached, reachable only through our wrapped
routes, and our override of allauth's template kept in step with allauth."""

from __future__ import annotations

import hashlib
import re
import secrets
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import allauth
import pytest
from allauth.mfa.recovery_codes import views as recovery_views
from allauth.mfa.totp import views as totp_views
from django.test import Client
from django.urls import URLPattern, URLResolver, get_resolver, resolve, reverse

from apps.accounts.tests.factories import UserFactory
from apps.accounts.tests.helpers import mfa_login, mfa_reauthenticate, submit_totp
from config import urls as project_urls

PASSWORD = secrets.token_urlsafe(16)
ROOT = Path(__file__).resolve().parents[3]
OVERRIDE = ROOT / "templates" / "mfa" / "totp" / "activate_form.html"
ALLAUTH_TEMPLATE = (
    Path(allauth.__file__).parent / "templates" / "mfa" / "totp" / "activate_form.html"
)

# allauth view -> the URL name of OUR wrapped route for it.
SECRET_VIEWS = {
    totp_views.activate_totp: "mfa_activate_totp",
    recovery_views.view_recovery_codes: "mfa_view_recovery_codes",
    recovery_views.generate_recovery_codes: "mfa_generate_recovery_codes",
    recovery_views.download_recovery_codes: "mfa_download_recovery_codes",
}


def _walk(patterns: list[Any], prefix: str = "") -> Iterator[tuple[str, URLPattern]]:
    for p in patterns:
        if isinstance(p, URLResolver):
            yield from _walk(p.url_patterns, prefix + str(p.pattern))
        elif isinstance(p, URLPattern):
            yield prefix + str(p.pattern), p


def _view_class(view: Any) -> Any:
    """The class behind an as_view() function; wrappers (never_cache...) keep it."""
    cls = getattr(view, "view_class", None)
    assert cls is not None, view
    return cls


def _ours(name: str) -> Any:
    for p in (*project_urls._TOTP_ROUTES, *project_urls._RECOVERY_CODE_ROUTES):
        if p.name == name:
            return p.callback
    raise AssertionError(f"no project route named {name}")


# --------------------------------------------------------------------------- routes


@pytest.mark.parametrize(
    ("view", "name"), list(SECRET_VIEWS.items()), ids=list(SECRET_VIEWS.values())
)
def test_the_allauth_view_is_reachable_only_through_our_route(view: Any, name: str) -> None:
    """allauth's own route for the view still exists (include), at the same path:
    the path must resolve to OUR wrapper, and no other path may lead to the view."""
    ours = _ours(name)
    paths = {
        "/" + route
        for route, pattern in _walk(get_resolver().url_patterns)
        if getattr(pattern.callback, "view_class", None) is _view_class(view)
    }
    assert paths == {reverse(name)}, paths
    for path in paths:
        assert resolve(path).func is ours, path
    assert ours is not view  # wrapped: never_cache (and the recent second factor)


@pytest.mark.parametrize("name", list(SECRET_VIEWS.values()))
def test_every_secret_page_is_wrapped_by_our_no_store(name: str) -> None:
    """Structural: allauth already sends no-store on some of these views (view and
    download of the recovery codes) but not on others (activation, generation), and
    may change: our own wrapper must be on every route, whatever allauth does."""
    assert getattr(resolve(reverse(name)).func, "secret_page_no_store", False) is True


def test_reverse_points_to_the_new_activation_route() -> None:
    assert reverse("mfa_activate_totp") == "/accounts/2fa/totp/activate/"
    match = resolve(reverse("mfa_activate_totp"))
    assert match.func is _ours("mfa_activate_totp")
    assert _view_class(match.func) is _view_class(totp_views.activate_totp)


# --------------------------------------------------------------------------- no-store


@pytest.fixture
def signed_in(db: Any, time_machine: Any) -> tuple[Client, Any]:
    """Signed in, then explicitly re-authenticated with the NEXT TOTP code, as allauth
    requires before showing recovery codes (the sign-in code cannot be replayed)."""
    import datetime as dt

    from allauth.mfa.recovery_codes.internal.auth import RecoveryCodes

    t0 = dt.datetime(2026, 12, 1, 10, 0, tzinfo=dt.UTC)
    user = UserFactory.create(password=PASSWORD)  # TOTP enrolled
    RecoveryCodes.activate(user)
    client = Client()
    time_machine.move_to(t0, tick=True)
    mfa_login(client, user, PASSWORD)
    time_machine.move_to(t0 + dt.timedelta(minutes=1), tick=True)
    mfa_reauthenticate(client, user)
    return client, user


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("method", "name"),
    [
        ("get", "mfa_view_recovery_codes"),
        ("get", "mfa_generate_recovery_codes"),
        ("post", "mfa_generate_recovery_codes"),  # regenerates, then shows the new codes
        ("get", "mfa_download_recovery_codes"),
    ],
)
def test_recovery_code_pages_are_never_cached(
    signed_in: tuple[Client, Any], method: str, name: str
) -> None:
    client, _user = signed_in
    response = getattr(client, method)(reverse(name))
    assert "no-store" in response["Cache-Control"], (name, response.get("Cache-Control"))
    if method == "post":
        assert response.status_code == 302
        assert response["Location"] == reverse("mfa_view_recovery_codes")
        response = client.get(response["Location"])  # the page with the new codes
        assert "no-store" in response["Cache-Control"]
    assert response.status_code == 200, (response.status_code, response.get("Location"))


@pytest.mark.django_db
def test_the_codes_are_really_on_the_page_that_is_never_cached(
    signed_in: tuple[Client, Any],
) -> None:
    """Control: the page under test does show the codes (else no-store proves nothing)."""
    from allauth.mfa.models import Authenticator
    from allauth.mfa.recovery_codes.internal.auth import RecoveryCodes

    client, user = signed_in
    response = client.get(reverse("mfa_download_recovery_codes"))
    codes = RecoveryCodes(Authenticator.objects.get(user=user, type="recovery_codes"))
    assert any(code in response.content.decode() for code in codes.get_unused_codes())
    assert "no-store" in response["Cache-Control"]


# --------------------------------------------------------------------------- override vs allauth


def _noted() -> tuple[str, str]:
    header = OVERRIDE.read_text().splitlines()[0]
    match = re.search(r"allauth (\d+\.\d+\.\d+), source sha256 ([0-9a-f]{64})", header)
    assert match, f"the override must note the allauth version and source hash: {header!r}"
    return match.group(1), match.group(2)


def test_the_override_was_reviewed_for_the_installed_allauth() -> None:
    """Fails on an allauth upgrade until someone re-reads allauth's template, ports
    any change into ours, and updates the version and hash noted in its first line."""
    version, digest = _noted()
    installed = hashlib.sha256(ALLAUTH_TEMPLATE.read_bytes()).hexdigest()
    assert allauth.__version__ == version, (
        f"allauth {allauth.__version__} installed, override reviewed for {version}: "
        "review templates/mfa/totp/activate_form.html"
    )
    assert installed == digest, "allauth's activate_form.html changed: review the override"


def test_the_override_differs_from_allauth_only_by_the_qr_code() -> None:
    ours = OVERRIDE.read_text().splitlines()[1:]  # without the header line
    theirs = ALLAUTH_TEMPLATE.read_text().splitlines()
    removed = [line for line in theirs if line not in ours]
    added = [line for line in ours if line not in theirs]
    # ("{% endelement %}" is on other lines of the template too: only the img line goes.)
    assert [line.strip() for line in removed] == [
        '{% element img src=totp_svg_data_uri alt=form.secret tags="mfa,totp,qr" %}'
    ]
    assert len(added) == 3 and 'class="qr"' in added[-1]


@pytest.mark.django_db(transaction=True)  # sign-in audited, then the vault seals the secret
def test_the_new_activation_route_still_works_end_to_end() -> None:
    """The wrapped view is the real one: activating a factor through it succeeds."""
    from allauth.mfa.totp.internal.auth import SECRET_SESSION_KEY

    user = UserFactory.create(password=PASSWORD, mfa=False)
    client = Client()
    client.post(reverse("account_login"), {"login": user.email, "password": PASSWORD})
    client.get(reverse("mfa_activate_totp"))
    if client.session.get(SECRET_SESSION_KEY) is None:
        client.post(reverse("account_reauthenticate"), {"password": PASSWORD})
        client.get(reverse("mfa_activate_totp"))
    secret = client.session[SECRET_SESSION_KEY]
    response = submit_totp(client, reverse("mfa_activate_totp"), secret)
    assert response.status_code == 302
