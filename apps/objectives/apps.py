from typing import Any

from django.apps import AppConfig


def sync_years_after_save(sender: Any, instance: Any, raw: bool = False, **kwargs: Any) -> None:
    """Keep the ThesisYear rows of a thesis in step with its dates (post_save of Thesis).

    In the same transaction as the save: a thesis is never committed without its
    years. Skipped for raw saves (loaddata), where related rows come from the
    fixture itself.
    """
    if raw:
        return
    from .services import sync_years

    sync_years(instance)


class ObjectivesConfig(AppConfig):
    name = "apps.objectives"
    label = "objectives"
    verbose_name = "Objectives"

    def ready(self) -> None:
        from django.db.models.signals import post_save

        from apps.accounts.models import Thesis

        post_save.connect(
            sync_years_after_save, sender=Thesis, dispatch_uid="objectives.sync_years"
        )
