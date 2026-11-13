from django.apps import AppConfig


def _document_versions_using(kek_id: str) -> int:
    from .models import DocumentVersion

    return DocumentVersion.objects.filter(kek_id=kek_id).count()


class VaultConfig(AppConfig):
    name = "apps.vault"
    label = "vault"
    verbose_name = "Vault"

    def ready(self) -> None:
        from . import keys
        from .formats import LEVEL_TO_DOMAIN

        # Refuse to start with an unknown or inconsistent key provider.
        keys.check_settings()
        # Document versions reference the KEKs of the four level domains.
        for domain in LEVEL_TO_DOMAIN.values():
            keys.register_reference_counter(domain, _document_versions_using)
