"""Signed checkpoints of the audit chain (docs/SECURITY.md).

A checkpoint states "the chain ended at ``seq`` with ``hash``", signed with
Ed25519 (vault.keys, key derived from CHECKPOINT_INFO). The signing key is on
the server that holds the chain, so a checkpoint protects only once copied
OUTSIDE: daily export file pulled by another machine, weekly e-mail to the PhD
student. audit_verify checks them with the public key PINNED outside the
platform (first export), never with a key the server provides.

Signed message (fixed length, unambiguous):
    b"these-audit-checkpoint-v1\\x00" | seq (8 bytes, big-endian)
    | hash (32 bytes) | timestamp (microseconds since the Unix epoch, 8 bytes, signed)
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Protocol

from django.db import transaction
from django.utils import timezone

from . import crypto, keys, locks
from .audit import AuditAction, Finding, audit
from .chain_v1 import format_timestamp
from .errors import AuditError
from .formats import canonical_json
from .models import AuditCheckpoint, AuditEvent

if TYPE_CHECKING:
    from collections.abc import Iterable

    from apps.accounts.models import User

CONTEXT = b"these-audit-checkpoint-v1\x00"
EXPORT_FORMAT = 1
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_TIMESTAMP = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z")
_HEX64 = re.compile(r"[0-9a-f]{64}")
_HEX128 = re.compile(r"[0-9a-f]{128}")

__all__ = ["create_checkpoint", "export_line", "format_timestamp", "parse_line", "verify"]


class CheckpointLike(Protocol):
    @property
    def seq(self) -> int: ...

    @property
    def hash(self) -> str: ...

    @property
    def timestamp(self) -> datetime: ...

    @property
    def signature(self) -> str: ...


@dataclass(frozen=True)
class ExternalCheckpoint:
    seq: int
    hash: str
    timestamp: datetime
    signature: str


def checkpoint_message(checkpoint: CheckpointLike) -> bytes:
    micros = (checkpoint.timestamp - _EPOCH) // timedelta(microseconds=1)
    return (
        CONTEXT
        + checkpoint.seq.to_bytes(8, "big")
        + bytes.fromhex(checkpoint.hash)
        + micros.to_bytes(8, "big", signed=True)
    )


def create_checkpoint(*, actor: User | None = None) -> AuditCheckpoint:
    """Sign the current end of the chain; the creation is audited (the NEXT link)."""
    with transaction.atomic():
        locks.acquire_audit_lock()  # the end of the chain cannot move meanwhile
        last = AuditEvent.objects.order_by("-seq").first()
        if last is None:
            raise AuditError("the audit log is empty: nothing to checkpoint")
        checkpoint = AuditCheckpoint(seq=last.seq, hash=last.hash, timestamp=timezone.now())
        checkpoint.signature = keys.sign_checkpoint(checkpoint_message(checkpoint)).hex()
        checkpoint.save(force_insert=True)
        audit(AuditAction.AUDIT_CHECKPOINT_CREATED, actor=actor, checkpoint_seq=last.seq)
    return checkpoint


def export_line(checkpoint: CheckpointLike) -> str:
    """One line of the external export file (canonical JSON)."""
    return canonical_json(
        {
            "v": EXPORT_FORMAT,
            "seq": checkpoint.seq,
            "hash": checkpoint.hash,
            "signature": checkpoint.signature,
            "timestamp": format_timestamp(checkpoint.timestamp),
        }
    ).decode("utf-8")


def parse_line(line: str) -> ExternalCheckpoint:
    """Strict parse of an export line; ValueError says what is wrong."""
    try:
        data = json.loads(line)
    except json.JSONDecodeError as exc:
        raise ValueError(f"not JSON ({exc.msg})") from None
    if not isinstance(data, dict) or set(data) != {"v", "seq", "hash", "signature", "timestamp"}:
        raise ValueError("expected exactly the keys v, seq, hash, signature, timestamp")
    if data["v"] != EXPORT_FORMAT:
        raise ValueError(f"unknown export format {data['v']!r}")
    seq, hash_, signature, stamp = data["seq"], data["hash"], data["signature"], data["timestamp"]
    if type(seq) is not int or seq < 1:
        raise ValueError("seq must be a positive integer")
    if not isinstance(hash_, str) or not _HEX64.fullmatch(hash_):
        raise ValueError("hash must be 64 lower-case hex digits")
    if not isinstance(signature, str) or not _HEX128.fullmatch(signature):
        raise ValueError("signature must be 128 lower-case hex digits")
    if not isinstance(stamp, str) or not _TIMESTAMP.fullmatch(stamp):
        raise ValueError("timestamp must be YYYY-MM-DDTHH:MM:SS.ffffffZ")
    moment = datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=UTC)
    return ExternalCheckpoint(seq=seq, hash=hash_, timestamp=moment, signature=signature)


def verify(
    checkpoints: Iterable[CheckpointLike],
    *,
    public_key: bytes,
    chain_hashes: dict[int, str],
    last_seq: int,
    label: str,
) -> list[Finding]:
    """Check each checkpoint's signature with the PINNED key, then against the chain.
    A checkpoint beyond the end of the chain reveals a tail truncation."""
    findings = []
    for checkpoint in checkpoints:
        prefix = f"{label} seq {checkpoint.seq}"
        message = checkpoint_message(checkpoint)
        if not crypto.ed25519_verify(public_key, bytes.fromhex(checkpoint.signature), message):
            findings.append(Finding("ALTERATION", f"{prefix}: invalid signature"))
        elif checkpoint.seq > last_seq:
            findings.append(
                Finding(
                    "ALTERATION",
                    f"{prefix}: beyond the end of the chain (truncated after seq {last_seq})",
                )
            )
        elif checkpoint.seq not in chain_hashes:
            findings.append(Finding("ALTERATION", f"{prefix}: no event with this seq"))
        elif not crypto.digests_equal(
            checkpoint.hash.encode(), chain_hashes[checkpoint.seq].encode()
        ):
            findings.append(Finding("ALTERATION", f"{prefix}: hash differs from the chain"))
    return findings
