"""Settings for mypy's django-stubs plugin only (pyproject.toml), never to run a server.

The plugin imports the settings and sets Django up to learn the models: it needs
the test applications, not a database, Redis or secrets. Every variable base.py
requires is replaced here by a fictitious value (servers on port 1, never
reachable; keys drawn at random on each run), and no file is read: mypy passes
in an empty environment (tests/test_test_database.py).
"""

import base64
import os
import secrets

os.environ["DJANGO_SECRET_KEY"] = secrets.token_urlsafe(50)
os.environ["MASTER_KEY"] = base64.b64encode(secrets.token_bytes(32)).decode()
os.environ["DATABASE_URL"] = "postgres://typecheck@127.0.0.1:1/typecheck"
os.environ["REDIS_URL"] = "redis://127.0.0.1:1/0"
os.environ["DJANGO_ALLOWED_HOSTS"] = "localhost"

from .base import *

# The applications of the test settings: mypy checks their models too.
INSTALLED_APPS = [*INSTALLED_APPS, "django.contrib.admin", "apps.demo"]
