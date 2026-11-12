"""Test settings (pytest): PostgreSQL, fast Argon2, eager Celery, admin enabled."""

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

from . import base  # noqa: E402
from .base import *  # noqa: E402
from .guards import require_local_hosts  # noqa: E402

# Check the hosts coming from the environment, before overriding them below.
require_local_hosts(base.ALLOWED_HOSTS, "config.settings.test")

DEBUG = False
ALLOWED_HOSTS = ["testserver", "localhost"]

ADMIN_ENABLED = True
INSTALLED_APPS = [*INSTALLED_APPS, "django.contrib.admin"]

# Still Argon2 first, with low cost parameters so the suite stays fast.
PASSWORD_HASHERS = ["config.test_hashers.FastArgon2PasswordHasher", *PASSWORD_HASHERS]

EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"

CELERY_TASK_ALWAYS_EAGER = True
CELERY_TASK_EAGER_PROPAGATES = True
