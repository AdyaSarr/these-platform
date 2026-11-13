"""PostgreSQL advisory locks of the vault, and the order in which they are taken.

Transaction-scoped advisory locks (``pg_advisory_xact_lock``) identified by two
int4 keys: a namespace and a key inside it.

ORDER (CLAUDE.md): KEK locks (vault.keys) BEFORE the audit-log lock.
``audit()`` takes the audit-log lock and is the last call of a transaction, so
a transaction never waits for a KEK lock while holding the audit-log lock (two
transactions taking them in opposite orders would deadlock). vault.keys
enforces it: a KEK lock that could make it wait is refused once the audit-log
lock is held.
"""

from __future__ import annotations

from django.db import connection

KEK_NAMESPACE = 0x564B  # "VK": one key per key domain (its frozen code)
AUDIT_NAMESPACE = 0x5641  # "VA"
AUDIT_CHAIN_KEY = 1

SHARED = "ShareLock"  # pg_locks.mode of pg_advisory_xact_lock_shared
EXCLUSIVE = "ExclusiveLock"  # pg_locks.mode of pg_advisory_xact_lock


def held(namespace: int, key: int) -> frozenset[str]:
    """Modes in which THIS transaction holds the advisory lock (namespace, key)."""
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT mode FROM pg_locks WHERE locktype = 'advisory' AND pid = pg_backend_pid() "
            "AND granted AND classid = %s AND objid = %s AND objsubid = 2",
            [namespace, key],
        )
        return frozenset(row[0] for row in cursor.fetchall())


def acquire(namespace: int, key: int, *, exclusive: bool) -> None:
    with connection.cursor() as cursor:
        if exclusive:
            cursor.execute("SELECT pg_advisory_xact_lock(%s, %s)", [namespace, key])
        else:
            cursor.execute("SELECT pg_advisory_xact_lock_shared(%s, %s)", [namespace, key])


def acquire_audit_lock() -> None:
    """Serializes audit-log writers until commit (the hash chain needs it).

    Outside a transaction the lock would be released at once: nothing to do.
    """
    if connection.in_atomic_block:
        acquire(AUDIT_NAMESPACE, AUDIT_CHAIN_KEY, exclusive=True)
