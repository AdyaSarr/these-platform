"""Acceptance: prod.py applies every setting of docs/SECURITY.md; check --deploy is clean."""

from typing import Any

import pytest

from tests.prod_helpers import PROD_HOST, load_prod_settings, prod_env, run_manage, run_python

# Long enough, but still the placeholder of .env.example.
PLACEHOLDER_KEY = "change-me-" + "x" * 60  # pragma: allowlist secret

SETTING_NAMES = (
    "DEBUG",
    "ALLOWED_HOSTS",
    "CSRF_TRUSTED_ORIGINS",
    "SECURE_SSL_REDIRECT",
    "SECURE_PROXY_SSL_HEADER",
    "SECURE_HSTS_SECONDS",
    "SECURE_HSTS_INCLUDE_SUBDOMAINS",
    "SECURE_HSTS_PRELOAD",
    "SESSION_COOKIE_SECURE",
    "CSRF_COOKIE_SECURE",
    "SESSION_COOKIE_SAMESITE",
    "SESSION_COOKIE_HTTPONLY",
    "SESSION_COOKIE_AGE",
    "SESSION_COOKIE_NAME",
    "CSRF_COOKIE_NAME",
    "X_FRAME_OPTIONS",
    "SECURE_REFERRER_POLICY",
    "SECURE_CONTENT_TYPE_NOSNIFF",
    "CONTENT_SECURITY_POLICY",
    "PASSWORD_HASHERS",
    "INSTALLED_APPS",
    "EMAIL_BACKEND",
)


@pytest.fixture(scope="module")
def prod() -> dict[str, Any]:
    return load_prod_settings(*SETTING_NAMES)


def test_debug_is_false(prod: dict[str, Any]) -> None:
    assert prod["DEBUG"] is False


def test_allowed_hosts_explicit(prod: dict[str, Any]) -> None:
    assert prod["ALLOWED_HOSTS"] == [PROD_HOST]
    assert prod["CSRF_TRUSTED_ORIGINS"] == [f"https://{PROD_HOST}"]


def test_https_and_hsts(prod: dict[str, Any]) -> None:
    assert prod["SECURE_SSL_REDIRECT"] is True
    assert prod["SECURE_PROXY_SSL_HEADER"] == ["HTTP_X_FORWARDED_PROTO", "https"]
    assert prod["SECURE_HSTS_SECONDS"] == 31_536_000
    assert prod["SECURE_HSTS_INCLUDE_SUBDOMAINS"] is True
    assert prod["SECURE_HSTS_PRELOAD"] is True


def test_secure_cookies(prod: dict[str, Any]) -> None:
    assert prod["SESSION_COOKIE_SECURE"] is True
    assert prod["CSRF_COOKIE_SECURE"] is True
    assert prod["SESSION_COOKIE_HTTPONLY"] is True
    assert prod["SESSION_COOKIE_SAMESITE"] == "Strict"
    assert prod["SESSION_COOKIE_AGE"] == 8 * 3600


def test_cookie_names_use_host_prefix(prod: dict[str, Any]) -> None:
    assert prod["SESSION_COOKIE_NAME"] == "__Host-sessionid"
    assert prod["CSRF_COOKIE_NAME"] == "__Host-csrftoken"


def test_framing_referrer_nosniff(prod: dict[str, Any]) -> None:
    assert prod["X_FRAME_OPTIONS"] == "DENY"
    assert prod["SECURE_REFERRER_POLICY"] == "same-origin"
    assert prod["SECURE_CONTENT_TYPE_NOSNIFF"] is True


def test_csp_strict(prod: dict[str, Any]) -> None:
    directives = prod["CONTENT_SECURITY_POLICY"]["DIRECTIVES"]
    assert directives["default-src"] == ["'none'"]
    assert directives["script-src"] == ["'self'"]
    flat = str(directives)
    assert "unsafe-inline" not in flat
    assert "unsafe-eval" not in flat


def test_argon2_first(prod: dict[str, Any]) -> None:
    assert prod["PASSWORD_HASHERS"][0] == "django.contrib.auth.hashers.Argon2PasswordHasher"


def test_smtp_email_backend(prod: dict[str, Any]) -> None:
    assert prod["EMAIL_BACKEND"] == "django.core.mail.backends.smtp.EmailBackend"


def test_admin_route_does_not_exist_in_prod(prod: dict[str, Any]) -> None:
    """Until M1 (MFA) is done, the Django admin must not be reachable in prod."""
    assert "django.contrib.admin" not in prod["INSTALLED_APPS"]
    assert prod["_admin_resolves"] is False
    assert prod["_admin_reversible"] is False


@pytest.mark.parametrize(
    ("overrides", "expected_in_error"),
    [
        ({"DJANGO_SECRET_KEY": None}, "DJANGO_SECRET_KEY"),
        ({"DJANGO_SECRET_KEY": "too-short"}, "DJANGO_SECRET_KEY"),  # pragma: allowlist secret
        ({"DJANGO_SECRET_KEY": PLACEHOLDER_KEY}, "DJANGO_SECRET_KEY"),
        ({"DJANGO_ALLOWED_HOSTS": "*"}, "DJANGO_ALLOWED_HOSTS"),
        ({"DJANGO_ALLOWED_HOSTS": ""}, "DJANGO_ALLOWED_HOSTS"),
        ({"DJANGO_SITE_URL": None}, "DJANGO_SITE_URL"),
        ({"DJANGO_SITE_URL": f"http://{PROD_HOST}"}, "DJANGO_SITE_URL"),
        ({"MASTER_KEY": None}, "MASTER_KEY"),
        ({"MASTER_KEY": ""}, "MASTER_KEY"),
        ({"MASTER_KEY": "bm90LTMyLWJ5dGVz"}, "MASTER_KEY"),  # "not-32-bytes"
        ({"MASTER_KEY": "A" * 43 + "!"}, "MASTER_KEY"),  # not base64
        ({"MASTER_KEY": "QUFB" * 12}, "MASTER_KEY"),  # 36 bytes
    ],
)
def test_prod_refuses_unsafe_environment(
    overrides: dict[str, str | None], expected_in_error: str
) -> None:
    result = run_python(
        "import django; django.setup()",
        prod_env(**overrides),
    )
    assert result.returncode != 0
    assert expected_in_error in result.stderr


def test_check_deploy_has_no_warning() -> None:
    result = run_manage("check", "--deploy", "--fail-level", "WARNING", env=prod_env())
    assert result.returncode == 0, result.stdout + result.stderr
    assert "System check identified no issues" in result.stdout
