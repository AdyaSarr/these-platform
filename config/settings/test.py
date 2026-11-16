"""Test settings (pytest): PostgreSQL, fast Argon2, eager Celery, admin enabled."""

import base64
import os
import secrets
from pathlib import Path

import environ

# Locally, DATABASE_URL and REDIS_URL come from .env; in CI from real variables,
# which take precedence over the file.
_ENV_FILE = Path(__file__).resolve().parent.parent.parent / ".env"
if _ENV_FILE.exists():
    environ.Env.read_env(_ENV_FILE)
# A fresh random key per run: no test secret is ever written down.
os.environ.setdefault("DJANGO_SECRET_KEY", secrets.token_urlsafe(50))
os.environ.setdefault("MASTER_KEY", base64.b64encode(secrets.token_bytes(32)).decode())

from . import base  # noqa: E402
from .base import *  # noqa: E402
from .guards import (  # noqa: E402
    require_local_database,
    require_local_hosts,
    require_scheme,
    required,
)

# Check the hosts coming from the environment, before overriding them below.
require_local_hosts(base.ALLOWED_HOSTS, "config.settings.test")

# The tests run on their own server (docker compose service db-test: data in
# memory, no fsync; docs/decisions.md), never on the development one: required,
# with no fall back to DATABASE_URL, and local only.
DATABASES = {
    "default": environ.Env.db_url_config(
        require_scheme(
            "TEST_DATABASE_URL",
            required("TEST_DATABASE_URL", "a postgres:// URL of the db-test service"),
            ("postgres", "postgresql", "pgsql"),
            "a postgres:// URL with user, password, host, port and database name",
        )
    )
}
require_local_database("TEST_DATABASE_URL", DATABASES["default"])
DATABASES["default"]["CONN_MAX_AGE"] = 60
DATABASES["default"]["CONN_HEALTH_CHECKS"] = True

DEBUG = False
ALLOWED_HOSTS = ["testserver", "localhost"]

ADMIN_ENABLED = True
INSTALLED_APPS = [*INSTALLED_APPS, "django.contrib.admin", "apps.demo"]

# Still Argon2 first, with low cost parameters so the suite stays fast.
PASSWORD_HASHERS = ["config.test_hashers.FastArgon2PasswordHasher", *PASSWORD_HASHERS]

EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"

CELERY_TASK_ALWAYS_EAGER = True
CELERY_TASK_EAGER_PROPAGATES = True
