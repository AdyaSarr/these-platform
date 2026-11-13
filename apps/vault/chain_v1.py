"""Audit hash chain, format v1. FROZEN: never modify this module (CLAUDE.md).

Stored rows were hashed with exactly this code; changing it would make the
whole existing chain fail verification. A new format goes into chain_v2.py,
with its own CHAIN_FORMAT; each row stores its format (AuditEvent.chain_format,
hashed as "v") and verification picks the module from it.

    hash = SHA-256(prev_hash (32 raw bytes) || canonical_json(payload))
    payload = {"v": 1, "seq", "timestamp", "actor", "thesis", "action",
               "object_type", "object_id", "ip_hash", "details"}

formats.canonical_json (RFC 8785 subset) is part of v1 as well: it is frozen
in the same way.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any, Protocol

from . import crypto
from .formats import canonical_json

CHAIN_FORMAT = 1
GENESIS_HASH = "0" * 64


class ChainRow(Protocol):
    @property
    def seq(self) -> int: ...

    @property
    def timestamp(self) -> datetime: ...

    @property
    def actor_id(self) -> Any: ...

    @property
    def thesis_id(self) -> Any: ...

    @property
    def action(self) -> str: ...

    @property
    def object_type(self) -> str: ...

    @property
    def object_id(self) -> str: ...

    @property
    def ip_hash(self) -> str: ...

    @property
    def details_json(self) -> Any: ...


def format_timestamp(moment: datetime) -> str:
    """YYYY-MM-DDTHH:MM:SS.ffffffZ, in UTC, always 6 fractional digits."""
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _uuid_text(value: Any) -> str | None:
    return None if value is None else str(uuid.UUID(str(value)))  # lower case, hyphens


def payload_bytes(row: ChainRow) -> bytes:
    """The canonical bytes hashed for ``row`` (a new or a stored one)."""
    return canonical_json(
        {
            "v": CHAIN_FORMAT,
            "seq": row.seq,
            "timestamp": format_timestamp(row.timestamp),
            "actor": _uuid_text(row.actor_id),
            "thesis": _uuid_text(row.thesis_id),
            "action": row.action,
            "object_type": row.object_type,
            "object_id": row.object_id,
            "ip_hash": row.ip_hash,
            "details": row.details_json,
        }
    )


def chain_hash(prev_hash: str, payload: bytes) -> str:
    """SHA-256(prev_hash raw bytes || payload), lower-case hex (vault.crypto.sha256:
    plain SHA-256, CLAUDE.md rule 1)."""
    return crypto.sha256(bytes.fromhex(prev_hash) + payload).hex()
