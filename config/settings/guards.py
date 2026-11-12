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
