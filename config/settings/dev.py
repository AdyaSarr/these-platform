"""Local development: reads .env, enables DEBUG and the admin."""

from pathlib import Path

import environ

# Fill os.environ from .env before base.py reads it. Variables already set in the
# shell take precedence.
_ENV_FILE = Path(__file__).resolve().parent.parent.parent / ".env"
if _ENV_FILE.exists():
    environ.Env.read_env(_ENV_FILE)

from .base import *  # noqa: E402
from .guards import require_local_hosts  # noqa: E402

require_local_hosts(ALLOWED_HOSTS, "config.settings.dev")

DEBUG = True

ADMIN_ENABLED = True
INSTALLED_APPS = [*INSTALLED_APPS, "django.contrib.admin"]

EMAIL_BACKEND = "django.core.mail.backends.console.EmailBackend"

# WebAuthn needs a secure context; http://localhost is accepted in dev only.
MFA_WEBAUTHN_ALLOW_INSECURE_ORIGIN = True
