"""Deployment checks with the database (vault.E001: MFA secret round trip through the
vault; vault.E002: every key domain has a KEK version).

Run by `manage.py check --deploy --database default` (docs/DEPLOYMENT.md). Django
runs checks tagged "database" only when --database is given, so check --deploy
without a database (prod settings tests) is unaffected.
"""

from __future__ import annotations

from typing import Any

from django.core.checks import CheckMessage, Error, Tags, register


@register(Tags.database, deploy=True)
def mfa_secret_round_trip(app_configs: Any, **kwargs: Any) -> list[CheckMessage]:
    databases = kwargs.get("databases") or ()
    if "default" not in databases:
        return []
    from . import services

    probe = "vault-deploy-check"
    try:
        ok = services.decrypt_secret(services.encrypt_secret(probe)) == probe
        failure = "the decrypted value differs"
    except Exception as exc:  # reported, not raised: a check lists problems
        ok, failure = False, f"{type(exc).__name__}: {exc}"
    if ok:
        return []
    return [
        Error(
            f"MFA secret round trip through the vault failed ({failure}).",
            hint="Run `manage.py vault_init`; check MASTER_KEY and VAULT_KEY_PROVIDER.",
            id="vault.E001",
        )
    ]


@register(Tags.database, deploy=True)
def every_domain_has_a_kek(app_configs: Any, **kwargs: Any) -> list[CheckMessage]:
    databases = kwargs.get("databases") or ()
    if "default" not in databases:
        return []
    from . import keys

    missing = keys.missing_domains()
    if not missing:
        return []
    return [
        Error(
            "Key domains without any KEK version: " + ", ".join(d.value for d in missing) + ".",
            hint="Run `manage.py vault_init` (before any user enrolls a second factor).",
            id="vault.E002",
        )
    ]
