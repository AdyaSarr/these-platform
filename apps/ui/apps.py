from django.apps import AppConfig


class UiConfig(AppConfig):
    """Visual layer: navigation, theme, icons, dashboard tiles, style guide.

    Presentation only: every right comes from apps.accounts.permissions.
    """

    name = "apps.ui"
    label = "ui"
