from django.apps import AppConfig


class PortfolioConfig(AppConfig):
    name = "apps.portfolio"
    label = "portfolio"
    verbose_name = "Portfolio"

    def ready(self) -> None:
        # M9b: the publication details are sealed at their submission's level; a change
        # of that level reseals them too (apps/conferences/sealed.py).
        from apps.conferences import sealed

        from .models import DETAILS_TEXTS, PublicationDetails

        sealed.register(PublicationDetails, DETAILS_TEXTS)
