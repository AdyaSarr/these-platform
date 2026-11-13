"""Audit checkpoints: signature, external export, public key export, weekly e-mail."""

import json
import os
import stat
from datetime import UTC, datetime, timedelta
from io import StringIO
from pathlib import Path
from typing import Any

import pytest
from django.core import mail
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import DatabaseError, connection, transaction

from apps.accounts.roles import Role
from apps.accounts.tests.factories import MembershipFactory, ThesisFactory
from apps.vault import checkpoints, crypto, keys
from apps.vault.audit import AuditAction, audit
from apps.vault.models import AuditCheckpoint, AuditEvent

pytestmark = pytest.mark.django_db

T0 = datetime(2026, 11, 13, 9, 0, 0, 654321, tzinfo=UTC)


def test_checkpoint_signs_the_end_of_the_chain(time_machine: Any) -> None:
    time_machine.move_to(T0, tick=False)
    last = audit(AuditAction.LOGIN_FAILED)
    checkpoint = checkpoints.create_checkpoint()
    assert (checkpoint.seq, checkpoint.hash) == (last.seq, last.hash)
    message = (
        b"these-audit-checkpoint-v1\x00"
        + last.seq.to_bytes(8, "big")
        + bytes.fromhex(last.hash)
        + ((T0 - datetime(1970, 1, 1, tzinfo=UTC)) // timedelta(microseconds=1)).to_bytes(
            8, "big", signed=True
        )
    )
    assert checkpoints.checkpoint_message(checkpoint) == message
    assert crypto.ed25519_verify(
        keys.checkpoint_public_key(), bytes.fromhex(checkpoint.signature), message
    )
    after = AuditEvent.objects.order_by("-seq").first()
    assert after is not None
    assert after.action == "AUDIT_CHECKPOINT_CREATED"
    assert after.details_json == {"checkpoint_seq": last.seq}


def test_nothing_to_checkpoint_on_an_empty_log() -> None:
    with pytest.raises(CommandError, match="empty"):
        call_command("audit_checkpoint", stdout=StringIO())


def test_export_appends_one_canonical_line_in_a_private_file(tmp_path: Path) -> None:
    audit(AuditAction.LOGIN_FAILED)
    path = tmp_path / "out" / "checkpoints.jsonl"
    path.parent.mkdir()
    call_command("audit_checkpoint", "--export", str(path), stdout=StringIO())
    call_command("audit_checkpoint", "--export", str(path), stdout=StringIO())
    lines = path.read_text().splitlines()
    assert len(lines) == 2
    first = json.loads(lines[0])
    assert set(first) == {"v", "seq", "hash", "signature", "timestamp"}
    assert lines[0] == checkpoints.export_line(AuditCheckpoint.objects.get(seq=first["seq"]))
    assert checkpoints.parse_line(lines[0]).seq == first["seq"]
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600


def test_public_key_export_shows_the_fingerprint_and_is_audited_each_time() -> None:
    public = keys.checkpoint_public_key()
    for _ in range(2):
        out = StringIO()
        call_command("audit_checkpoint", "--export-public-key", stdout=out)
        assert public.hex() in out.getvalue()
        assert f"SHA-256 fingerprint: {crypto.sha256(public).hex()}" in out.getvalue()
    assert AuditEvent.objects.filter(action="AUDIT_PUBLIC_KEY_EXPORTED").count() == 2
    assert not AuditCheckpoint.objects.exists()  # exporting the key creates no checkpoint


def test_weekly_email_holds_only_the_four_fields_and_goes_to_students_only() -> None:
    thesis = ThesisFactory.create(title="Titre fictif à ne jamais envoyer")
    student = MembershipFactory.create(thesis=thesis, role=Role.PHD_STUDENT).user
    MembershipFactory.create(thesis=thesis, role=Role.DIRECTOR)
    other = MembershipFactory.create(thesis=ThesisFactory.create(), role=Role.PHD_STUDENT).user
    audit(AuditAction.LOGIN_FAILED)
    call_command("audit_checkpoint", "--email", stdout=StringIO())
    checkpoint = AuditCheckpoint.objects.get()
    assert sorted(m.to[0] for m in mail.outbox) == sorted([student.email, other.email])
    expected = (
        "Point de contrôle du journal d'audit de la plateforme.\n"
        "\n"
        f"seq : {checkpoint.seq}\n"
        f"hash : {checkpoint.hash}\n"
        f"signature : {checkpoint.signature}\n"
        f"horodatage : {checkpoints.format_timestamp(checkpoint.timestamp)}\n"
        "\n"
        "Conservez ce message hors de la plateforme : il permet de vérifier que le journal\n"
        "n'a pas été réécrit (manage.py audit_verify).\n"
    )
    for message in mail.outbox:
        assert message.subject == "Point de contrôle du journal d'audit"
        assert message.body == expected
        assert not getattr(message, "alternatives", []) and not message.attachments
        for leak in (
            thesis.title,
            student.last_name,
            student.first_name,
            keys.checkpoint_public_key().hex(),
        ):
            assert leak not in message.body
            assert leak not in message.subject


# --------------------------------------------------------------------------- append-only


def _sql(statement: str, params: list[Any] | None = None) -> None:
    with transaction.atomic(), connection.cursor() as cursor:
        # Deferred foreign-key checks pending in the test transaction would make
        # PostgreSQL refuse TRUNCATE before our trigger is reached.
        cursor.execute("SET CONSTRAINTS ALL IMMEDIATE")
        cursor.execute(statement, params or [])


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE vault_auditcheckpoint SET signature = repeat('0', 128)",
        "DELETE FROM vault_auditcheckpoint",
        "TRUNCATE vault_auditcheckpoint",
    ],
)
def test_checkpoints_are_append_only(statement: str) -> None:
    audit(AuditAction.LOGIN_FAILED)
    checkpoints.create_checkpoint()
    with pytest.raises(DatabaseError, match="append-only"):
        _sql(statement)


def test_checkpoint_must_match_its_event() -> None:
    event = audit(AuditAction.LOGIN_FAILED)
    with pytest.raises(DatabaseError, match="does not match"):
        _sql(
            "INSERT INTO vault_auditcheckpoint (seq, hash, timestamp, signature) "
            "VALUES (%s, %s, now(), repeat('0', 128))",
            [event.seq, "e" * 64],
        )
