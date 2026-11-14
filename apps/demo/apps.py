from django.apps import AppConfig


class DemoConfig(AppConfig):
    """Demonstration thesis (manage.py demo_seed). Installed by config.settings.demo
    and config.settings.test only: the command does not even exist elsewhere."""

    name = "apps.demo"
    label = "demo"
