"""DROP / CREATE of the demonstration database, for scripts/demo_reset.sh only.

A database can only be dropped from another one: these functions connect to the
server's maintenance database (``postgres``) with the credentials of
DATABASES["default"]. They refuse any name that does not end with ``_demo``.
"""

from __future__ import annotations

import re

import psycopg
from django.conf import settings
from psycopg import sql

DEMO_NAME = re.compile(r"[a-z][a-z0-9_]{0,50}_demo")
MAINTENANCE_DB = "postgres"


def checked_name(name: str) -> str:
    if not isinstance(name, str) or not DEMO_NAME.fullmatch(name):
        raise ValueError(f"refused: {name!r} is not the name of a demonstration database (_demo)")
    return name


def _connect() -> psycopg.Connection[tuple[object, ...]]:
    db = settings.DATABASES["default"]
    return psycopg.connect(
        dbname=MAINTENANCE_DB,
        host=db.get("HOST") or None,
        port=str(db.get("PORT") or "") or None,
        user=db.get("USER") or None,
        password=db.get("PASSWORD") or None,
        autocommit=True,  # DROP / CREATE DATABASE cannot run in a transaction
        connect_timeout=10,
    )


def exists(name: str) -> bool:
    with _connect() as conn:
        row = conn.execute(
            "SELECT 1 FROM pg_database WHERE datname = %s", [checked_name(name)]
        ).fetchone()
    return row is not None


def drop(name: str) -> None:
    statement = sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(
        sql.Identifier(checked_name(name))
    )
    with _connect() as conn:
        conn.execute(statement)


def create(name: str) -> None:
    statement = sql.SQL("CREATE DATABASE {}").format(sql.Identifier(checked_name(name)))
    with _connect() as conn:
        conn.execute(statement)
