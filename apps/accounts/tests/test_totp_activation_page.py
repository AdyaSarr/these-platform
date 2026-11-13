"""The TOTP activation page (/accounts/2fa/totp/activate/), as a browser gets it.

Bug seen in Chrome: allauth renders the QR code as <img src="data:image/svg+xml;...">,
which our CSP (img-src 'self') blocks; the browser then shows the alt text, which
was the TOTP secret. The QR code is now an inline SVG, checked against a strict
allow-list, and the page must render under the CSP it actually sends.
"""

from __future__ import annotations

import logging
import re
import secrets
from typing import Any

import pytest
from allauth.mfa.totp.internal.auth import SECRET_SESSION_KEY
from django.test import Client
from django.urls import reverse

from apps.accounts import qr
from apps.accounts.tests.factories import UserFactory, totp_code

pytestmark = pytest.mark.django_db(transaction=True)

PASSWORD = secrets.token_urlsafe(16)
NEUTRAL_LABEL = "Code QR de configuration"


def _csp(response: Any) -> dict[str, list[str]]:
    header = response["Content-Security-Policy"]
    directives: dict[str, list[str]] = {}
    for part in header.split(";"):
        tokens = part.split()
        if tokens:
            directives[tokens[0].lower()] = tokens[1:]
    return directives


def _sources(csp: dict[str, list[str]], directive: str) -> list[str]:
    return csp.get(directive, csp.get("default-src", []))


def _page(caplog: pytest.LogCaptureFixture | None = None) -> tuple[Client, Any, str]:
    """A signed-in user without a factor, on the activation page: (client, response, secret)."""
    user = UserFactory.create(password=PASSWORD, mfa=False)
    client = Client()
    client.post(reverse("account_login"), {"login": user.email, "password": PASSWORD})
    response = client.get(reverse("mfa_activate_totp"))
    if client.session.get(SECRET_SESSION_KEY) is None:  # allauth asked to re-authenticate
        client.post(reverse("account_reauthenticate"), {"password": PASSWORD})
        response = client.get(reverse("mfa_activate_totp"))
    assert response.status_code == 200
    return client, response, client.session[SECRET_SESSION_KEY]


def test_the_qr_code_renders_under_the_csp_the_page_sends() -> None:
    _client, response, _secret = _page()
    html = response.content.decode()
    csp = _csp(response)
    images = re.findall(r"<img\b[^>]*>", html)
    for img in images:  # any <img> must load under img-src
        src = re.search(r'src="([^"]*)"', img)
        assert src is not None
        if src.group(1).startswith("data:"):
            assert "data:" in _sources(csp, "img-src"), img
    svg = re.search(r'<div class="qr"[^>]*>\s*(<svg\b.*?</svg>)\s*</div>', html, re.S)
    assert svg is not None, "no inline QR code"
    markup = svg.group(1)
    # Inline SVG: nothing that style-src 'self' / script-src 'self' would block.
    assert "'unsafe-inline'" not in _sources(csp, "style-src")
    assert not re.search(r"\sstyle\s*=", markup) and "<style" not in markup
    assert "<script" not in markup and not re.search(r"\son[a-z]+\s*=", markup)
    assert markup.count("<path") >= 1


def test_the_site_policy_is_not_weakened_on_this_page() -> None:
    _client, response, _secret = _page()
    csp = _csp(response)
    assert _sources(csp, "img-src") == ["'self'"]
    assert all("'unsafe-inline'" not in v and "data:" not in v for v in csp.values())


def test_the_secret_is_only_in_its_field() -> None:
    _client, response, secret = _page()
    html = response.content.decode()
    for alt in re.findall(r'\balt="([^"]*)"', html):
        assert secret not in alt
    assert NEUTRAL_LABEL in html
    assert html.count(secret) == 1  # the read-only "Authenticator secret" field
    field = re.search(r'<input[^>]*id="authenticator_secret"[^>]*>', html)
    assert field is not None and secret in field.group(0)


def test_the_page_is_never_cached() -> None:
    _client, response, _secret = _page()
    assert "no-store" in response["Cache-Control"]


def test_the_referrer_policy_lets_the_form_post() -> None:
    """The page holds a POST form: no-referrer would make the browser send Origin: null."""
    _client, response, _secret = _page()
    assert response["Referrer-Policy"] != "no-referrer"


def test_activation_posted_like_a_browser_works_and_logs_no_secret(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    user = UserFactory.create(password=PASSWORD, mfa=False)
    browser = Client(enforce_csrf_checks=True)
    login = browser.get(reverse("account_login"))
    token = re.search(r'name="csrfmiddlewaretoken" value="([^"]+)"', login.content.decode())
    assert token
    browser.post(
        reverse("account_login"),
        {"login": user.email, "password": PASSWORD, "csrfmiddlewaretoken": token.group(1)},
        HTTP_ORIGIN="http://testserver",
    )
    page = browser.get(reverse("mfa_activate_totp"))
    if page.status_code == 302:  # allauth asks for the password again first
        reauth = browser.get(page["Location"])
        token = re.search(r'name="csrfmiddlewaretoken" value="([^"]+)"', reauth.content.decode())
        assert token
        browser.post(
            page["Location"],
            {"password": PASSWORD, "csrfmiddlewaretoken": token.group(1)},
            HTTP_ORIGIN="http://testserver",
        )
        page = browser.get(reverse("mfa_activate_totp"))
    secret = browser.session.get(SECRET_SESSION_KEY)
    assert secret, page.status_code
    token = re.search(r'name="csrfmiddlewaretoken" value="([^"]+)"', page.content.decode())
    assert token
    response = browser.post(
        reverse("mfa_activate_totp"),
        {"code": totp_code(secret), "csrfmiddlewaretoken": token.group(1)},
        HTTP_ORIGIN="http://testserver",
    )
    assert response.status_code == 302, response.content[:300]
    logged = " ".join(f"{r.getMessage()} {r.args}" for r in caplog.records)
    assert secret not in logged


# --------------------------------------------------------------------------- the allow-list


def test_the_generated_qr_code_passes_the_allow_list() -> None:
    from allauth.mfa.adapter import get_adapter
    from allauth.mfa.totp.internal.auth import generate_totp_secret

    secret = str(generate_totp_secret())  # a fresh random secret: none written down
    svg = get_adapter().build_totp_svg(
        f"otpauth://totp/Fictif:fictif%40example.org?secret={secret}&issuer=Fictif"
    )
    assert svg.startswith("<svg")  # the XML declaration is gone
    assert secret not in svg  # the secret is only in the modules, not in text


@pytest.mark.parametrize(
    "svg",
    [
        '<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>',
        '<svg xmlns="http://www.w3.org/2000/svg" onload="alert(1)"><path d="M0,0z"/></svg>',
        '<svg xmlns="http://www.w3.org/2000/svg"><path d="M0,0z" style="fill:red"/></svg>',
        '<svg xmlns="http://www.w3.org/2000/svg"><style>path{}</style></svg>',
        '<svg xmlns="http://www.w3.org/2000/svg"><a href="javascript:x"><path d="M0z"/></a></svg>',
        '<svg xmlns="http://www.w3.org/2000/svg"><foreignObject>x</foreignObject></svg>',
        '<svg xmlns="http://www.w3.org/2000/svg">texte libre<path d="M0,0z"/></svg>',
        '<svg xmlns="http://www.w3.org/2000/svg"><path d="M0,0z" fill="url(http://x/)"/></svg>',
        '<svg xmlns="http://www.w3.org/2000/svg"><path d="M0,0z" fill=\'x"\'/></svg>',
        "<!DOCTYPE svg><svg></svg>",
        "",
    ],
)
def test_anything_outside_the_allow_list_is_refused(svg: str) -> None:
    with pytest.raises(ValueError):
        qr.inline_svg(svg)


def test_a_refused_qr_code_falls_back_to_the_secret_field(monkeypatch: Any) -> None:
    """If the SVG ever changes shape, the page still works (manual entry), without it."""
    from allauth.mfa.adapter import DefaultMFAAdapter

    monkeypatch.setattr(
        DefaultMFAAdapter, "build_totp_svg", lambda self, url: "<svg><script></script></svg>"
    )
    _client, response, secret = _page()
    html = response.content.decode()
    assert '<div class="qr"' not in html and "<script></script>" not in html
    assert secret in html  # still in its field
