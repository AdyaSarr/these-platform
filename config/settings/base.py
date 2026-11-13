"""Settings shared by every environment.

Values come from environment variables only (no .env file is read here: see
dev.py and test.py). Secure defaults: an environment loosens them, never the
reverse.
"""

from datetime import timedelta
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
    "django.contrib.humanize",  # used by allauth's WebAuthn key list
    # PostgreSQL-only features: exclusion constraints on membership periods.
    "django.contrib.postgres",
    "django_htmx",
    "csp",
    "apps.core",
    "apps.accounts",
    "apps.vault",
    "apps.admin_file",
    # Authentication: sign-in, password reset, mandatory second factor (M1).
    "allauth",
    "allauth.account",
    "allauth.mfa",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.locale.LocaleMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "allauth.account.middleware.AccountMiddleware",
    # After authentication: no page without an enrolled second factor.
    "apps.accounts.middleware.MfaRequiredMiddleware",
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

# allauth's backend (a ModelBackend subclass): e-mail login, rate limiting.
AUTHENTICATION_BACKENDS = ["allauth.account.auth_backends.AuthenticationBackend"]

LOGIN_URL = "/accounts/login/"
LOGIN_REDIRECT_URL = "core:home"
LOGOUT_REDIRECT_URL = "login"

# --------------------------------------------------------------------------- allauth (M1)
# Invitation only: sign-up is closed (AccountAdapter.is_open_for_signup).
ACCOUNT_ADAPTER = "apps.accounts.adapters.AccountAdapter"
ACCOUNT_LOGIN_METHODS = {"email"}
ACCOUNT_SIGNUP_FIELDS = ["email*", "password1*"]
ACCOUNT_USER_MODEL_USERNAME_FIELD = None
# The invitation proves the address; e-mail changes are disabled (decisions.md).
ACCOUNT_EMAIL_VERIFICATION = "none"
ACCOUNT_CHANGE_EMAIL = False
ACCOUNT_MAX_EMAIL_ADDRESSES = 1
# No account enumeration: same answers whether the address exists or not, and
# no e-mail at all to an unknown address.
ACCOUNT_PREVENT_ENUMERATION = True
ACCOUNT_EMAIL_UNKNOWN_ACCOUNTS = False
ACCOUNT_LOGIN_BY_CODE_ENABLED = False
ACCOUNT_PASSWORD_RESET_BY_CODE_ENABLED = False
ACCOUNT_LOGIN_ON_PASSWORD_RESET = False  # a reset never bypasses the second factor
ACCOUNT_SESSION_REMEMBER = False
ACCOUNT_EMAIL_SUBJECT_PREFIX = ""
# Re-authentication: password or second factor (never a code sent by e-mail).
ACCOUNT_REAUTHENTICATION_REQUIRED = True
ACCOUNT_REAUTHENTICATION_METHODS = ("password", "mfa")
ACCOUNT_REAUTHENTICATION_TIMEOUT = 600  # seconds; also apps/accounts/reauth.py
# Rate limits stay at allauth's defaults (login_failed: 10/min/IP, 5/5 min per
# address). They live in the cache: CACHES must be shared in production.

MFA_ADAPTER = "apps.accounts.adapters.MFAAdapter"
MFA_SUPPORTED_TYPES = ["totp", "webauthn", "recovery_codes"]
MFA_PASSKEY_LOGIN_ENABLED = False  # WebAuthn as a second factor only
MFA_PASSKEY_SIGNUP_ENABLED = False
MFA_TRUST_ENABLED = False  # no "trust this browser": the factor is asked every time
MFA_TOTP_ISSUER = "Plateforme de thèse"
MFA_RECOVERY_CODE_COUNT = 10
# Codes are shown once, right after generation; afterwards only regenerating
# (second factor < 10 min, config/urls.py) gives new ones.
MFA_RECOVERY_CODES_SHOW_ONCE = True
# Production refuses to start while MFA secrets are stored unencrypted
# (MFAAdapter.encrypt is the identity until M2). See config/wsgi.py.
MFA_ENCRYPTION_REQUIRED = False

# --------------------------------------------------------------------------- cache
# Per-process memory cache outside production (single runserver / test process).
CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}

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

# Error reports (technical 500 page, e-mails to ADMINS, logs) hide the locals of
# @sensitive_variables functions even with DEBUG=True (Django's filter only
# acts when DEBUG is False). See apps/core/debug.py.
DEFAULT_EXCEPTION_REPORTER_FILTER = "apps.core.debug.AlwaysSafeExceptionReporterFilter"

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
# Seconds before an SMTP connection or reply gives up (Django's default: None,
# wait forever). A stuck server would otherwise keep a reminder's row locked
# (admin_file.tasks) and a worker busy for ever. prod.py sets the same value.
EMAIL_TIMEOUT = 10

# Root key of the vault (base64, 32 bytes). Sub-keys are derived from it by HKDF in
# apps/vault/crypto.py only. Never displayed: Django masks settings named *KEY*.
MASTER_KEY = env.str("MASTER_KEY", default="")

# Key provider for NEW wraps, and providers allowed to UNwrap (chosen by the
# kek_id prefix). Checked at start-up (apps/vault/apps.py). After a migration to
# OpenBao and a complete rotation, remove "env" (docs/DEPLOYMENT.md).
VAULT_KEY_PROVIDER = "env"
VAULT_ENABLED_PROVIDERS = frozenset({"env"})
# A KEK version can be retired only once the NEXT version has existed this long
# (data sealed outside the shared lock: allauth's encrypt(), open()'s read window).
VAULT_RETIRE_GRACE = timedelta(minutes=10)

# Encrypted blobs (apps/vault/storage.py). Outside every directory a web server
# serves; checked at start-up. STRICT (prod): absolute path required, and wide
# permissions on the directory refuse to run instead of only warning.
VAULT_STORAGE_DIR = env.str("VAULT_STORAGE_DIR", default=str(BASE_DIR / "var" / "vault"))
VAULT_STORAGE_STRICT = False

# Uploads stay in memory, capped at 25 MB per request: never spooled in clear to
# a temporary file (apps/vault/upload_handlers.py, docs/SECURITY.md).
FILE_UPLOAD_HANDLERS = ["apps.vault.upload_handlers.LimitedMemoryUploadHandler"]

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
    # SQL queries carry wrapped DEKs and document titles: never logged with their
    # parameters, whatever the root level or DEBUG (tests/test_security_audit_m2.py).
    "loggers": {"django.db.backends": {"level": "INFO", "propagate": True}},
}
