"""manage.py audit_verify: modification, deletion, insertion, rewrite, truncation.

Alterations are simulated as a database administrator would make them: the
append-only triggers are disabled for the duration of the change.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from io import StringIO
from pathlib import Path
from typing import Any

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection

from apps.vault import audit as audit_module
from apps.vault import checkpoints, crypto, keys
from apps.vault.audit import AuditAction, audit
from apps.vault.models import AuditEvent

pytestmark = pytest.mark.django_db

A = AuditAction


@contextmanager
def dba(table: str = "vault_auditevent") -> Iterator[Any]:
    with connection.cursor() as cursor:
        # Deferred foreign-key checks of rows inserted in this test would forbid ALTER TABLE.
        cursor.execute("SET CONSTRAINTS ALL IMMEDIATE")
        cursor.execute(f"ALTER TABLE {table} DISABLE TRIGGER USER")
        try:
            yield cursor
        finally:
            cursor.execute(f"ALTER TABLE {table} ENABLE TRIGGER USER")


def _events(n: int = 5) -> list[AuditEvent]:
    return [audit(A.LOGIN_FAILED, reason=f"fictitious {i}") for i in range(n)]


def _verify(*args: str) -> tuple[str, int]:
    out = StringIO()
    try:
        call_command("audit_verify", *args, stdout=out)
    except CommandError as exc:
        return out.getvalue(), exc.returncode
    return out.getvalue(), 0


def _pinned_key() -> str:
    return keys.checkpoint_public_key().hex()


def _export(tmp_path: Path, name: str = "checkpoints.jsonl") -> Path:
    """A checkpoint of the current end of the chain, exported outside the platform."""
    path = tmp_path / name
    with path.open("a") as f:
        f.write(checkpoints.export_line(checkpoints.create_checkpoint()) + "\n")
    return path


def _rehash_from(seq: int) -> None:
    """What an attacker with database access does after editing row ``seq``."""
    previous = AuditEvent.objects.get(seq=seq - 1).hash if seq > 1 else audit_module.GENESIS_HASH
    with dba() as cursor:
        for row in AuditEvent.objects.filter(seq__gte=seq).order_by("seq"):
            row.prev_hash = previous
            new = audit_module.chain_hash(previous, audit_module.payload_bytes(row))
            cursor.execute(
                "UPDATE vault_auditevent SET prev_hash = %s, hash = %s WHERE seq = %s",
                [previous, new, row.seq],
            )
            previous = new


# --------------------------------------------------------------------------- the chain alone


def test_clean_chain_without_checkpoints_warns_about_truncation() -> None:
    _events()
    out, code = _verify()
    assert code == 0
    assert "OK\t5 events" in out
    assert "WARNING\ttail truncation is not detectable without external checkpoints" in out


def test_modified_row_is_detected() -> None:
    _events()
    with dba() as cursor:
        cursor.execute(
            'UPDATE vault_auditevent SET details_json = \'{"reason": "edited"}\' WHERE seq = 3'
        )
    out, code = _verify()
    assert code == 2
    assert "ALTERATION\tseq 3: hash mismatch (row modified)" in out


def test_deleted_row_is_detected() -> None:
    _events()
    with dba() as cursor:
        cursor.execute("DELETE FROM vault_auditevent WHERE seq = 3")
    out, code = _verify()
    assert code == 2
    assert "ALTERATION\tseq 3: missing (expected after seq 2, found seq 4)" in out


def test_row_inserted_in_the_middle_is_detected() -> None:
    events = _events()
    with dba() as cursor:
        for seq in (5, 4, 3):
            cursor.execute("UPDATE vault_auditevent SET seq = seq + 1 WHERE seq = %s", [seq])
    forged = AuditEvent(
        seq=3,
        timestamp=events[2].timestamp,
        action="LOGOUT",
        details_json={},
        prev_hash=events[1].hash,
    )
    forged_hash = audit_module.chain_hash(forged.prev_hash, audit_module.payload_bytes(forged))
    with dba() as cursor:
        cursor.execute(
            "INSERT INTO vault_auditevent (seq, timestamp, action, object_type, object_id, "
            "ip_hash, details_json, prev_hash, hash) VALUES (3, %s, 'LOGOUT', '', '', '', "
            "'{}', %s, %s)",
            [forged.timestamp, forged.prev_hash, forged_hash],
        )
    out, code = _verify()
    assert code == 2
    assert "ALTERATION\tseq 4: chain broken (prev_hash" in out


def test_full_rewrite_needs_external_checkpoints(tmp_path: Path) -> None:
    _events()
    exported = _export(tmp_path)  # covers seq 5, taken before the attack
    with dba() as cursor:
        cursor.execute(
            'UPDATE vault_auditevent SET details_json = \'{"reason": "edited"}\' WHERE seq = 3'
        )
    _rehash_from(3)
    out, code = _verify()
    assert code == 0  # undetectable from the chain alone...
    assert "WARNING" in out
    out, code = _verify("--checkpoints", str(exported), "--public-key", _pinned_key())
    assert code == 2  # ...but not against an external checkpoint
    assert "ALTERATION\tcheckpoint seq 5: hash differs from the chain" in out


def test_tail_truncation_is_detected_by_an_external_checkpoint(tmp_path: Path) -> None:
    _events()
    exported = _export(tmp_path)  # seq 5
    with dba() as cursor:
        cursor.execute("DELETE FROM vault_auditevent WHERE seq >= 5")
    assert _verify()[1] == 0
    out, code = _verify("--checkpoints", str(exported), "--public-key", _pinned_key())
    assert code == 2
    assert "ALTERATION\tcheckpoint seq 5: beyond the end of the chain (truncated after seq" in out


# --------------------------------------------------------------------------- pinned key


def test_checkpoints_are_verified_with_the_pinned_key_only(tmp_path: Path) -> None:
    """A compromised server signs with ITS key: only the key pinned outside counts.
    Mutation target: verification with the key the server computes."""
    _events()
    exported = _export(tmp_path)
    reference = crypto.ed25519_public_key(b"\x07" * 32).hex()  # not the server's key
    out, code = _verify("--checkpoints", str(exported), "--public-key", reference)
    assert code == 2
    assert "ALTERATION\tcheckpoint seq 5: invalid signature" in out


def test_external_checkpoints_require_a_pinned_key(tmp_path: Path) -> None:
    _events()
    exported = _export(tmp_path)
    with pytest.raises(CommandError, match="--public-key"):
        call_command("audit_verify", "--checkpoints", str(exported), stdout=StringIO())


def test_public_key_may_be_read_from_a_file(tmp_path: Path) -> None:
    _events()
    exported = _export(tmp_path)
    key_file = tmp_path / "checkpoint-public-key.txt"
    key_file.write_text(_pinned_key() + "\n")
    out, code = _verify("--checkpoints", str(exported), "--public-key", str(key_file))
    assert code == 0, out
    assert "OK\t1 external checkpoint(s)" in out
    assert "WARNING" not in out


def test_database_checkpoints_are_only_a_complement(tmp_path: Path) -> None:
    _events()
    checkpoints.create_checkpoint()  # stored in the database, not exported
    out, code = _verify("--public-key", _pinned_key())
    assert code == 0
    assert "OK\t1 database checkpoint(s)" in out
    assert "WARNING\ttail truncation is not detectable without external checkpoints" in out


def test_malformed_checkpoint_file_is_refused(tmp_path: Path) -> None:
    _events()
    bad = tmp_path / "bad.jsonl"
    bad.write_text('{"seq": "five"}\n')
    with pytest.raises(CommandError, match="line 1"):
        call_command(
            "audit_verify",
            "--checkpoints",
            str(bad),
            "--public-key",
            _pinned_key(),
            stdout=StringIO(),
        )


# --------------------------------------------------------------------------- details


def test_timestamps_going_back_are_information_not_alteration(time_machine: Any) -> None:
    t0 = datetime(2026, 11, 13, 9, 0, tzinfo=UTC)
    time_machine.move_to(t0, tick=False)
    audit(A.LOGIN_FAILED)
    time_machine.move_to(t0 - timedelta(minutes=5), tick=False)  # clock set back
    audit(A.LOGIN_FAILED)
    out, code = _verify()
    assert code == 0
    assert "INFO\tseq 2: timestamp earlier than seq 1" in out


def test_hashes_are_compared_in_constant_time(monkeypatch: pytest.MonkeyPatch) -> None:
    """Mutation target: comparison with ==."""
    _events(3)
    calls: list[int] = []
    real = crypto.digests_equal

    def spy(a: bytes, b: bytes) -> bool:
        calls.append(1)
        return real(a, b)

    monkeypatch.setattr(crypto, "digests_equal", spy)
    assert _verify()[1] == 0
    assert len(calls) >= 6  # prev_hash and hash of each of the 3 events


def test_empty_log_is_fine() -> None:
    out, code = _verify()
    assert code == 0
    assert "OK\t0 events" in out
