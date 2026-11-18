"""Production: every setting of docs/SECURITY.md. Reads real environment variables
only (no .env file) and refuses to start with an unsafe configuration."""

from django.core.exceptions import ImproperlyConfigured

from .base import *
from .guards import require_master_key, require_scheme, required

DEBUG = False

if len(SECRET_KEY) < 50 or SECRET_KEY.startswith("change-me"):
    raise ImproperlyConfigured(
        "DJANGO_SECRET_KEY must be a random value of at least 50 characters "
        "(python -c 'import secrets; print(secrets.token_urlsafe(50))')."
    )
if not ALLOWED_HOSTS or "*" in ALLOWED_HOSTS:
    raise ImproperlyConfigured("DJANGO_ALLOWED_HOSTS must list explicit host names (no '*').")

CSRF_TRUSTED_ORIGINS = env.list("DJANGO_CSRF_TRUSTED_ORIGINS", default=[])

require_master_key(MASTER_KEY)

# Shared cache: allauth's rate limits and TOTP anti-replay markers live there.
# A per-process cache would let an attacker spread attempts over workers.
CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.redis.RedisCache",
        "LOCATION": CELERY_BROKER_URL,
        "KEY_PREFIX": "cache",
    }
}

MFA_ENCRYPTION_REQUIRED = True
MFA_TOTP_PERIOD = 30
MFA_TOTP_TOLERANCE = 1  # explicit here too: tested by tests/test_prod_settings.py
VAULT_STORAGE_STRICT = True

# Links in e-mails must point to the HTTPS site, never to the localhost default.
if not SITE_URL.startswith("https://"):
    raise ImproperlyConfigured("DJANGO_SITE_URL must be the public https:// URL of the platform.")

# --------------------------------------------------------------------------- HTTPS
SECURE_SSL_REDIRECT = True
# Caddy/Nginx terminates TLS and tells Django the original scheme.
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
# One proxy (Caddy) in front of Django: the client address is the last entry of
# X-Forwarded-For, the one the proxy appended; the earlier ones come from the
# client and are ignored. Read by allauth's rate limits, the audit ip_hash and the
# iCal limit, all through vault.audit.client_ip (SR-02, docs/DEPLOYMENT.md).
ALLAUTH_TRUSTED_PROXY_COUNT = 1
SECURE_HSTS_SECONDS = 31_536_000
SECURE_HSTS_INCLUDE_SUBDOMAINS = True
SECURE_HSTS_PRELOAD = True

# --------------------------------------------------------------------------- cookies
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True
# __Host-: the browser requires Secure, Path=/ and no Domain (docs/decisions.md).
SESSION_COOKIE_NAME = "__Host-sessionid"
CSRF_COOKIE_NAME = "__Host-csrftoken"

# --------------------------------------------------------------------------- static files
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.ManifestStaticFilesStorage"},
}

# --------------------------------------------------------------------------- e-mail
# Encrypted SMTP only: a console or dummy backend in production would print the
# e-mails to the logs or drop them silently, and plain smtp:// would carry the
# invitation and reset links (bearer tokens) and the SMTP password in clear (SR-03).
_email_url = require_scheme(
    "EMAIL_URL",
    required("EMAIL_URL", "an smtp+tls:// URL with user, password, host and port"),
    ("smtp+tls", "smtp+ssl"),
    "an SMTP URL with TLS: smtp+tls:// or smtp+ssl://, with user, password, host and port",
)
_email = env.email_url_config(_email_url)
EMAIL_BACKEND = _email["EMAIL_BACKEND"]
EMAIL_HOST = _email.get("EMAIL_HOST", "")
EMAIL_PORT = _email.get("EMAIL_PORT", 587)
EMAIL_HOST_USER = _email.get("EMAIL_HOST_USER", "")
EMAIL_HOST_PASSWORD = _email.get("EMAIL_HOST_PASSWORD", "")
EMAIL_USE_TLS = _email.get("EMAIL_USE_TLS", False)
EMAIL_USE_SSL = _email.get("EMAIL_USE_SSL", False)
EMAIL_TIMEOUT = 10

# The sender of every e-mail (invitations, reminders, summaries): an address of
# the platform's domain, never base.py's local default.
DEFAULT_FROM_EMAIL = required("DEFAULT_FROM_EMAIL", "e.g. noreply@these.example.org")
_sender_domain = DEFAULT_FROM_EMAIL.rpartition("@")[2].lower()
if (
    DEFAULT_FROM_EMAIL.count("@") != 1
    or any(c.isspace() for c in DEFAULT_FROM_EMAIL)
    or "." not in _sender_domain
    or _sender_domain in {"localhost", "localhost.localdomain"}
):
    raise ImproperlyConfigured(
        "DEFAULT_FROM_EMAIL must be an address of the platform's domain, "
        "e.g. noreply@these.example.org (see .env.example and docs/DEPLOYMENT.md)."
    )
