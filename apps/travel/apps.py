from django.apps import AppConfig


class TravelConfig(AppConfig):
    name = "apps.travel"
    label = "travel"
    verbose_name = "Travel"

    def ready(self) -> None:
        from apps.accounts.permissions import register_document_restriction

        from .access import official_order_restriction

        # The official mission order: readable only by those who see the mission.
        register_document_restriction(official_order_restriction)
