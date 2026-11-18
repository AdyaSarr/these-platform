"""Proofs of the configuration findings of the review (docs/security/revue-2026-11.md).

Production settings are loaded in a subprocess with fictitious values (as
tests/test_prod_settings.py does). Each test states the expected safe behaviour
and failed (xfail strict) until its finding was fixed; the markers are gone.
"""

from __future__ import annotations

from typing import Any

import pytest

from tests.prod_helpers import load_prod_settings


@pytest.fixture(scope="module")
def prod() -> dict[str, Any]:
    return load_prod_settings(
        "ALLAUTH_TRUSTED_PROXY_COUNT", "ALLAUTH_TRUSTED_CLIENT_IP_HEADER", "PASSWORD_RESET_TIMEOUT"
    )


def test_sr02_the_client_address_behind_the_proxy_is_configured(prod: dict[str, Any]) -> None:
    """allauth (login_failed 10/min/ip), the audit ip_hash and the iCal limit per
    address read REMOTE_ADDR unless allauth is told which proxy header to trust;
    nothing in the repository sets REMOTE_ADDR from the proxy either."""
    assert prod["ALLAUTH_TRUSTED_PROXY_COUNT"] or prod["ALLAUTH_TRUSTED_CLIENT_IP_HEADER"]


def test_sr03_production_refuses_smtp_without_tls() -> None:
    """Invitation and password-reset links (bearer tokens) would cross the network
    in clear between the server and the SMTP relay."""
    with pytest.raises(AssertionError):  # load_prod_settings asserts a clean start
        load_prod_settings(
            "EMAIL_USE_TLS",
            EMAIL_URL="smtp://fake:fake@smtp.example.org:25",  # pragma: allowlist secret
        )


def test_sr08_a_password_reset_link_is_short_lived(prod: dict[str, Any]) -> None:
    assert prod["PASSWORD_RESET_TIMEOUT"] <= 3600
