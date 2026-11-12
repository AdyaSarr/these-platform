"""Load config.settings.prod in a subprocess with fictitious environment values.

A subprocess keeps prod settings isolated from the test process (Django settings
can only be configured once per process). The environment is built from scratch:
nothing is inherited from the developer's shell or from .env.
"""

import base64
import json
import os
import secrets
import subprocess
import sys
from pathlib import Path
from typing import Any

BASE_DIR = Path(__file__).resolve().parent.parent

PROD_HOST = "these.example.org"


def prod_env(**overrides: str | None) -> dict[str, str]:
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": os.environ.get("HOME", ""),
        "DJANGO_SETTINGS_MODULE": "config.settings.prod",
        "DJANGO_SECRET_KEY": secrets.token_urlsafe(50),
        "DJANGO_ALLOWED_HOSTS": PROD_HOST,
        "DJANGO_CSRF_TRUSTED_ORIGINS": f"https://{PROD_HOST}",
        "DJANGO_SITE_URL": f"https://{PROD_HOST}",
        "MASTER_KEY": base64.b64encode(secrets.token_bytes(32)).decode(),
        # Port 1: never reachable; prod tests must not need a database.
        "DATABASE_URL": "postgres://fake:fake@127.0.0.1:1/fake",  # pragma: allowlist secret
        "REDIS_URL": "redis://:fake@127.0.0.1:1/0",  # pragma: allowlist secret
        "EMAIL_URL": "smtp+tls://fake:fake@smtp.example.org:587",  # pragma: allowlist secret
        "DEFAULT_FROM_EMAIL": f"noreply@{PROD_HOST}",
    }
    for key, value in overrides.items():
        if value is None:
            env.pop(key, None)
        else:
            env[key] = value
    return env


def run_python(code: str, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-c", code],
        cwd=BASE_DIR,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def run_manage(*args: str, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "manage.py", *args],
        cwd=BASE_DIR,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


_DUMP_SETTINGS = """
import json, django
django.setup()
from django.conf import settings
from django.urls import NoReverseMatch, Resolver404, resolve, reverse
names = {names!r}
out = {{n: getattr(settings, n, None) for n in names}}
try:
    resolve("/admin/")
    out["_admin_resolves"] = True
except Resolver404:
    out["_admin_resolves"] = False
try:
    reverse("admin:index")
    out["_admin_reversible"] = True
except NoReverseMatch:
    out["_admin_reversible"] = False
print(json.dumps(out, default=str))
"""


def load_prod_settings(*names: str, **env_overrides: str | None) -> dict[str, Any]:
    result = run_python(_DUMP_SETTINGS.format(names=list(names)), prod_env(**env_overrides))
    assert result.returncode == 0, result.stderr
    data: dict[str, Any] = json.loads(result.stdout.strip().splitlines()[-1])
    return data
