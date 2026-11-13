from django.apps import AppConfig


class VaultConfig(AppConfig):
    name = "apps.vault"
    label = "vault"
    verbose_name = "Vault"

    def ready(self) -> None:
        # Refuse to start with an unknown or inconsistent key provider.
        from . import keys

        keys.check_settings()
