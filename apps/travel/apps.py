from django.apps import AppConfig


class TravelConfig(AppConfig):
    name = "apps.travel"
    label = "travel"
    verbose_name = "Travel"

    def ready(self) -> None:
        from apps.accounts.permissions import register_document_restriction

        from .access import mission_document_restriction

        # A mission's documents (official order, receipts): readable only by those
        # who see the mission and read its level.
        register_document_restriction(mission_document_restriction)
