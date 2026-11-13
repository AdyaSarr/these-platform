"""Helpers for tests with real concurrent database connections (one per thread).

Waits are observed in pg_locks (granted = false), never with durations; each
thread gets a 10 s lock_timeout as a safety net.
"""

import threading
import time
from collections.abc import Callable

from django.db import connection


def backend_pid() -> int:
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_backend_pid()")
        return int(cursor.fetchone()[0])


def wait_until_blocked(
    pid: int, *, advisory_namespace: int | None = None, within: float = 5.0
) -> None:
    """Poll pg_locks until backend ``pid`` waits for a lock (an advisory lock of
    ``advisory_namespace`` if given, any lock otherwise)."""
    sql = "SELECT EXISTS (SELECT 1 FROM pg_locks WHERE pid = %s AND NOT granted"
    params: list[int] = [pid]
    if advisory_namespace is not None:
        sql += " AND locktype = 'advisory' AND classid = %s"
        params.append(advisory_namespace)
    deadline = time.monotonic() + within
    while time.monotonic() < deadline:
        with connection.cursor() as cursor:
            cursor.execute(sql + ")", params)
            if cursor.fetchone()[0]:
                return
        time.sleep(0.01)
    raise AssertionError(f"backend {pid} never waited for the expected lock")


def run_threads(*targets: Callable[[], None], control: Callable[[], None]) -> None:
    """Run ``targets`` in threads while ``control`` observes them from this thread;
    ``control`` must release every waiting thread in a ``finally``."""
    errors: list[BaseException] = []

    def guarded(target: Callable[[], None]) -> Callable[[], None]:
        def run() -> None:
            try:
                with connection.cursor() as cursor:
                    cursor.execute("SET lock_timeout = '10s'")
                target()
            except BaseException as exc:  # re-raised in the main thread
                errors.append(exc)
            finally:
                connection.close()

        return run

    threads = [threading.Thread(target=guarded(t)) for t in targets]
    for t in threads:
        t.start()
    try:
        control()
    finally:
        for t in threads:
            t.join(30)
    assert not any(t.is_alive() for t in threads)
    if errors:
        raise errors[0]
