from django.apps import AppConfig


def _document_versions_using(kek_id: str) -> int:
    from .models import DocumentVersion

    return DocumentVersion.objects.filter(kek_id=kek_id).count()


def _sealed_texts_using(kek_id: str) -> int:
    from .services import sealed_texts_using

    return sealed_texts_using(kek_id)


def _mfa_secrets_using(kek_id: str) -> int:
    from .services import mfa_secrets_using

    return mfa_secrets_using(kek_id)


class VaultConfig(AppConfig):
    name = "apps.vault"
    label = "vault"
    verbose_name = "Vault"

    def ready(self) -> None:
        from . import checks, keys, storage  # noqa: F401 - checks registers itself
        from .formats import LEVEL_TO_DOMAIN, KeyDomain

        # Refuse to start with an unknown or inconsistent key provider, or with
        # blobs inside a directory a web server serves.
        keys.check_settings()
        storage.check_settings()
        # Document versions and sealed texts reference the KEKs of the four level
        # domains; MFA secrets (allauth Authenticator rows) those of MFA_SECRETS.
        for domain in LEVEL_TO_DOMAIN.values():
            keys.register_reference_counter(domain, _document_versions_using)
            keys.register_reference_counter(domain, _sealed_texts_using)
        keys.register_reference_counter(KeyDomain.MFA_SECRETS, _mfa_secrets_using)
