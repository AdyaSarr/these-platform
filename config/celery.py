"""Celery application (background tasks: reminders from M6 on)."""

import os

from celery import Celery

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.dev")

app = Celery("config")
# Every Django setting prefixed CELERY_ configures Celery (e.g. CELERY_ACCEPT_CONTENT).
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks()
