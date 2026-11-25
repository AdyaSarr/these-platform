from django.apps import AppConfig


class ConferencesConfig(AppConfig):
    name = "apps.conferences"
    label = "conferences"
    verbose_name = "Conferences"

    def ready(self) -> None:
        # Texts sealed at a submission's level in other tables (apps/conferences/sealed.py).
        from . import sealed
        from .models import Preprint, StApproval

        sealed.register(StApproval, ("comment",))
        sealed.register(Preprint, ("url",))
