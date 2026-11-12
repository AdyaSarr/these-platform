"""Settings shared by every environment.

Values come from environment variables only (no .env file is read here: see
dev.py and test.py). Secure defaults: an environment loosens them, never the
reverse.
"""

from pathlib import Path

import environ
from csp.constants import NONE, SELF

BASE_DIR = Path(__file__).resolve().parent.parent.parent

env = environ.Env()

# --------------------------------------------------------------------------- core
SECRET_KEY = env.str("DJANGO_SECRET_KEY")
DEBUG = False
ALLOWED_HOSTS: list[str] = [h for h in env.list("DJANGO_ALLOWED_HOSTS", default=[]) if h]

# The Django admin has no MFA until M1: only dev.py and test.py enable it.
ADMIN_ENABLED = False

INSTALLED_APPS = [
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    # PostgreSQL-only features: exclusion constraints on membership periods.
    "django.contrib.postgres",
    "django_htmx",
    "csp",
    "apps.core",
    "apps.accounts",
    "apps.vault",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.locale.LocaleMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "csp.middleware.CSPMiddleware",
    "django_htmx.middleware.HtmxMiddleware",
]

ROOT_URLCONF = "config.urls"
WSGI_APPLICATION = "config.wsgi.application"
ASGI_APPLICATION = "config.asgi.application"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

# --------------------------------------------------------------------------- database
DATABASES = {"default": env.db_url("DATABASE_URL")}
DATABASES["default"]["CONN_MAX_AGE"] = 60
DATABASES["default"]["CONN_HEALTH_CHECKS"] = True
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# --------------------------------------------------------------------------- authentication
AUTH_USER_MODEL = "accounts.User"

# The first hasher hashes new passwords; the others only verify legacy hashes.
PASSWORD_HASHERS = [
    "django.contrib.auth.hashers.Argon2PasswordHasher",
    "django.contrib.auth.hashers.PBKDF2PasswordHasher",
    "django.contrib.auth.hashers.ScryptPasswordHasher",
]

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {
        "NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
        "OPTIONS": {"min_length": 12},
    },
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LOGIN_URL = "/accounts/login/"
LOGIN_REDIRECT_URL = "core:home"
LOGOUT_REDIRECT_URL = "login"

# --------------------------------------------------------------------------- sessions & cookies
SESSION_COOKIE_AGE = 8 * 3600
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Strict"
CSRF_COOKIE_SAMESITE = "Strict"

# --------------------------------------------------------------------------- security headers
X_FRAME_OPTIONS = "DENY"
SECURE_REFERRER_POLICY = "same-origin"
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_CROSS_ORIGIN_OPENER_POLICY = "same-origin"

# Strict CSP in every environment (docs/decisions.md): no inline script or style.
CONTENT_SECURITY_POLICY = {
    "DIRECTIVES": {
        "default-src": [NONE],
        "script-src": [SELF],
        "style-src": [SELF],
        "img-src": [SELF],
        "font-src": [SELF],
        "connect-src": [SELF],
        "form-action": [SELF],
        "frame-ancestors": [NONE],
        "base-uri": [NONE],
    },
}

# --------------------------------------------------------------------------- i18n
LANGUAGE_CODE = "fr-fr"
LANGUAGES = [("fr", "Français")]
TIME_ZONE = "Europe/Paris"
USE_I18N = True
USE_TZ = True
LOCALE_PATHS = [BASE_DIR / "locale"]

# --------------------------------------------------------------------------- static files
STATIC_URL = "static/"
STATICFILES_DIRS = [BASE_DIR / "static"]
STATIC_ROOT = BASE_DIR / "var" / "static"

# --------------------------------------------------------------------------- e-mail
DEFAULT_FROM_EMAIL = env.str("DEFAULT_FROM_EMAIL", default="noreply@localhost")

# Root key of the vault (base64, 32 bytes). Sub-keys are derived from it by HKDF in
# apps/vault/crypto.py only. Never displayed: Django masks settings named *KEY*.
MASTER_KEY = env.str("MASTER_KEY", default="")

# Absolute base URL of the platform, for links in e-mails (sent without a request).
SITE_URL = env.str("DJANGO_SITE_URL", default="http://localhost:8000").rstrip("/")

# --------------------------------------------------------------------------- celery
CELERY_BROKER_URL = env.str("REDIS_URL")
# JSON only: Celery must never deserialise pickle (CLAUDE.md rule 11).
CELERY_ACCEPT_CONTENT = ["json"]
CELERY_TASK_SERIALIZER = "json"
CELERY_RESULT_SERIALIZER = "json"
CELERY_TIMEZONE = TIME_ZONE

# --------------------------------------------------------------------------- logging
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {"plain": {"format": "{asctime} {levelname} {name} {message}", "style": "{"}},
    "handlers": {"console": {"class": "logging.StreamHandler", "formatter": "plain"}},
    "root": {"handlers": ["console"], "level": "INFO"},
}
