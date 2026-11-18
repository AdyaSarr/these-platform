"""Deployment checks with the database (vault.E001: MFA secret round trip through the
vault; vault.E002: every key domain has a KEK version; vault.E003: the role cannot
bypass the append-only triggers).

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


# The tables whose append-only triggers protect the audit log (vault migrations).
APPEND_ONLY_TABLES = ("vault_auditevent", "vault_auditcheckpoint")


@register(Tags.database, deploy=True)
def role_cannot_bypass_the_append_only_triggers(
    app_configs: Any, **kwargs: Any
) -> list[CheckMessage]:
    """The triggers stop only a role that cannot disable them (SR-07): a superuser
    (SET session_replication_role = replica) or the owner of the table, or a member
    of the owner's role (SET ROLE, then ALTER TABLE ... DISABLE TRIGGER)."""
    databases = kwargs.get("databases") or ()
    if "default" not in databases:
        return []
    from django.db import connections

    with connections["default"].cursor() as cursor:
        cursor.execute("SELECT rolsuper FROM pg_roles WHERE rolname = current_user")
        row = cursor.fetchone()
        superuser = bool(row and row[0])
        cursor.execute(
            "SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace"
            " WHERE c.relname = ANY(%s) AND n.nspname = current_schema()"
            " AND pg_has_role(current_user, c.relowner, 'MEMBER') ORDER BY c.relname",
            [list(APPEND_ONLY_TABLES)],
        )
        owned = [name for (name,) in cursor.fetchall()]
    problems = (["the database role is a superuser"] if superuser else []) + (
        [f"the database role owns (or is a member of the owner of) {', '.join(owned)}"]
        if owned
        else []
    )
    if not problems:
        return []
    return [
        Error(
            "The append-only triggers of the audit log can be bypassed: "
            + "; ".join(problems)
            + ".",
            hint="Run the application with a role that is neither superuser nor owner of the "
            "tables, with SELECT and INSERT only on them (docs/DEPLOYMENT.md, audit log roles).",
            id="vault.E003",
        )
    ]
