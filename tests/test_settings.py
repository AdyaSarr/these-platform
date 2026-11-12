"""Settings shared by every environment (base.py) and the test environment."""

from django.conf import settings
from django.contrib.auth.hashers import Argon2PasswordHasher
from django.urls import reverse
from django.utils.module_loading import import_string

from config.settings import base


def test_base_password_hashers_start_with_argon2() -> None:
    assert base.PASSWORD_HASHERS[0] == "django.contrib.auth.hashers.Argon2PasswordHasher"


def test_active_password_hasher_is_argon2() -> None:
    first = import_string(settings.PASSWORD_HASHERS[0])
    assert issubclass(first, Argon2PasswordHasher)


def test_locale_and_timezone() -> None:
    assert settings.LANGUAGE_CODE == "fr-fr"
    assert settings.TIME_ZONE == "Europe/Paris"
    assert settings.USE_TZ is True
    assert settings.USE_I18N is True


def test_custom_user_model() -> None:
    assert settings.AUTH_USER_MODEL == "accounts.User"


def test_session_defaults() -> None:
    assert settings.SESSION_COOKIE_AGE == 8 * 3600
    assert settings.SESSION_COOKIE_SAMESITE == "Strict"
    assert settings.SESSION_COOKIE_HTTPONLY is True


def test_cookie_names_are_default_outside_prod() -> None:
    """__Host- needs HTTPS, so it is set in prod.py only (docs/decisions.md)."""
    assert not hasattr(base, "SESSION_COOKIE_NAME")
    assert not hasattr(base, "CSRF_COOKIE_NAME")
    assert settings.SESSION_COOKIE_NAME == "sessionid"
    assert settings.CSRF_COOKIE_NAME == "csrftoken"


def test_csp_is_strict_in_base() -> None:
    directives = base.CONTENT_SECURITY_POLICY["DIRECTIVES"]
    assert directives["default-src"] == ["'none'"]
    assert directives["script-src"] == ["'self'"]
    assert directives["style-src"] == ["'self'"]
    assert directives["frame-ancestors"] == ["'none'"]
    assert directives["base-uri"] == ["'none'"]
    assert directives["form-action"] == ["'self'"]
    flat = str(directives)
    assert "unsafe-inline" not in flat
    assert "unsafe-eval" not in flat


def test_celery_never_accepts_pickle() -> None:
    from config.celery import app

    assert settings.CELERY_ACCEPT_CONTENT == ["json"]
    assert settings.CELERY_TASK_SERIALIZER == "json"
    assert settings.CELERY_RESULT_SERIALIZER == "json"
    assert list(app.conf.accept_content) == ["json"]


def test_admin_enabled_outside_prod() -> None:
    assert settings.ADMIN_ENABLED is True
    assert reverse("admin:index") == "/admin/"


def test_base_disables_admin() -> None:
    assert base.ADMIN_ENABLED is False
