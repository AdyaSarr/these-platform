"""Start-up guards shared by the settings modules."""

from django.core.exceptions import ImproperlyConfigured

LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "[::1]", "testserver"})


def require_local_hosts(hosts: list[str], module: str) -> None:
    """Refuse to load non-production settings in a production environment.

    A wrong DJANGO_SETTINGS_MODULE on a server would otherwise serve DEBUG pages
    (which expose settings such as CELERY_BROKER_URL) and the admin without MFA.
    The production environment always lists its public host name in
    DJANGO_ALLOWED_HOSTS, so that is what we detect.
    """
    public = sorted(set(hosts) - LOCAL_HOSTS)
    if public:
        raise ImproperlyConfigured(
            f"{module} only serves local hosts, but DJANGO_ALLOWED_HOSTS contains "
            f"{public}. Use DJANGO_SETTINGS_MODULE=config.settings.prod in production."
        )


MASTER_KEY_BYTES = 32


def require_master_key(value: str) -> None:
    """MASTER_KEY must decode (strict base64) to exactly 32 bytes.

    Only checks the format: the derivations live in apps/vault/crypto.py
    (CLAUDE.md rule 1). base64 is an encoding, not cryptography.
    """
    import base64
    import binascii

    try:
        decoded = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError):
        decoded = b""
    if len(decoded) != MASTER_KEY_BYTES:
        raise ImproperlyConfigured(
            "MASTER_KEY must be 32 random bytes encoded in base64 "
            '(python -c "import os,base64;print(base64.b64encode(os.urandom(32)).decode())").'
        )
