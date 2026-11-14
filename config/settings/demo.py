"""Demonstration database on a local machine (manage.py demo_seed, scripts/demo_reset.sh).

Development settings, with:
- the database of DATABASE_URL with the suffix ``_demo`` (same server and
  credentials, another database): the dev database is never touched;
- its own vault directory, var/vault-demo (the reset script deletes exactly this);
- Celery eager: tasks run in the process, no worker, nothing in the broker;
- the demo app, which provides demo_seed and demo_db.

MASTER_KEY and DJANGO_SECRET_KEY are those of dev (docs/decisions.md): the
data are fictional only.
"""

from .dev import *
from .dev import BASE_DIR, DATABASES, INSTALLED_APPS

DEMO_SUFFIX = "_demo"

_name = str(DATABASES["default"]["NAME"])
DATABASES["default"]["NAME"] = _name if _name.endswith(DEMO_SUFFIX) else _name + DEMO_SUFFIX

VAULT_STORAGE_DIR = str(BASE_DIR / "var" / "vault-demo")

CELERY_TASK_ALWAYS_EAGER = True
CELERY_TASK_EAGER_PROPAGATES = True

INSTALLED_APPS = [*INSTALLED_APPS, "apps.demo"]
