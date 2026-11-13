"""Hash chain of the audit log: payload, hashes, validator, append-only triggers."""

import hashlib
import itertools
import threading
import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from django.db import DatabaseError, connection, transaction

from apps.vault import audit as audit_module
from apps.vault import locks
from apps.vault.audit import GENESIS_HASH, AuditAction, audit
from apps.vault.errors import AuditError
from apps.vault.models import AuditEvent
from apps.vault.tests.concurrency import backend_pid, run_threads, wait_until_blocked

pytestmark = pytest.mark.django_db

A = AuditAction
T0 = datetime(2026, 11, 13, 9, 0, 0, 123456, tzinfo=UTC)


def _independent_hash(prev_hex: str, payload: bytes) -> str:
    """Written from docs/SECURITY.md, not from audit.py."""
    return hashlib.sha256(bytes.fromhex(prev_hex) + payload).hexdigest()


# --------------------------------------------------------------------------- payload and hash


def test_first_event_exact_payload_and_hash(time_machine: Any) -> None:
    time_machine.move_to(T0, tick=False)
    event = audit(A.LOGIN_FAILED, reason="fictitious")
    payload = (
        b'{"action":"LOGIN_FAILED","actor":null,"details":{"reason":"fictitious"},'
        b'"ip_hash":"","object_id":"","object_type":"","seq":1,"thesis":null,'
        b'"timestamp":"2026-11-13T09:00:00.123456Z","v":1}'
    )
    assert audit_module.payload_bytes(event) == payload
    assert event.seq == 1
    assert event.prev_hash == GENESIS_HASH == "0" * 64
    # Written out: SHA-256(32 zero bytes || payload), computed once outside the
    # project with a plain hashlib call on the bytes above.
    assert event.hash == (
        "e5c26b0b45a229f80bdfe5cea8daae9f"  # pragma: allowlist secret
        "8bed1a0d4e9cbf4f7cfcbe97b60054cb"  # pragma: allowlist secret
    )
    assert event.chain_format == 1


def test_events_are_linked_and_contiguous() -> None:
    events = [audit(A.LOGIN_FAILED, reason=str(i)) for i in range(5)]
    assert [e.seq for e in events] == [1, 2, 3, 4, 5]
    for previous, event in itertools.pairwise(events):
        assert event.prev_hash == previous.hash
        assert event.hash == _independent_hash(event.prev_hash, audit_module.payload_bytes(event))


def test_hash_of_a_stored_row_is_reproducible() -> None:
    from apps.accounts.tests.factories import MembershipFactory

    membership = MembershipFactory.create()
    audit(A.MEMBER_ROLE_CHANGED, actor=membership.user, obj=membership, old_role="a", new_role="b")
    row = AuditEvent.objects.get()  # read back from jsonb, timestamptz
    assert row.hash == _independent_hash(row.prev_hash, audit_module.payload_bytes(row))
    assert str(membership.user.pk) in audit_module.payload_bytes(row).decode()


def test_rolled_back_audit_leaves_no_gap() -> None:
    audit(A.LOGIN_FAILED)
    with pytest.raises(RuntimeError), transaction.atomic():
        audit(A.LOGIN_FAILED)
        raise RuntimeError("business failure after the audit")
    assert audit(A.LOGIN_FAILED).seq == 2


def test_timestamp_is_stored_in_utc_with_microseconds(time_machine: Any) -> None:
    time_machine.move_to(T0, tick=False)
    row = AuditEvent.objects.get(pk=audit(A.LOGIN_FAILED).pk)
    assert row.timestamp == T0
    assert '"timestamp":"2026-11-13T09:00:00.123456Z"' in audit_module.payload_bytes(row).decode()


def test_round_trip_through_postgresql_keeps_the_hash() -> None:
    """Stored as jsonb / timestamptz and read back, the row hashes to the same value."""
    emoji, line_separator = chr(0x1F600), chr(0x2028)  # outside the BMP; not escaped by JCS
    controls = "".join(chr(c) for c in (0x01, 0x08, 0x09, 0x0A, 0x0C, 0x0D, 0x1F, 0x7F))
    details: dict[str, Any] = {
        "accents": "Thèse : état de l'art, noël, ÇÀÉ",
        "emoji": emoji,
        "separator": "a" + line_separator + "b",
        "controls": controls,
        "slashes": "a/b\\c",
        "quotes": 'dit "x"',
        "big": 2**53 - 1,
        "small": -(2**53 - 1),
        "nested": [[1, [2, ["x", None]]], {"inner": [True, False, emoji]}],
        "empty": {"list": [], "object": {}},
    }
    event = audit(A.LOGIN_FAILED, **details)
    row = AuditEvent.objects.get(seq=event.seq)  # read back from PostgreSQL
    assert row.details_json == details
    assert audit_module.payload_bytes(row) == audit_module.payload_bytes(event)
    assert row.hash == _independent_hash(row.prev_hash, audit_module.payload_bytes(row))
    assert audit_module.verify_chain().findings == []


# --------------------------------------------------------------------------- validator


@pytest.mark.parametrize(
    "details",
    [
        {"reason": "a\x00b"},
        {"reason": "\ud800"},
        {"reason": ["ok", {"inner": "\udfff"}]},
        {"reason": 1.5},
        {"reason": 2**53},
    ],
    ids=["nul", "lone-high-surrogate", "nested-lone-low-surrogate", "float", "2^53"],
)
def test_details_values_outside_the_hashed_subset_are_refused(details: dict[str, Any]) -> None:
    with pytest.raises(AuditError):
        audit(A.LOGIN_FAILED, **details)
    assert not AuditEvent.objects.exists()


@pytest.mark.parametrize("key", ["Reason", "re-ason", "1reason", "raison_é", "_reason", "rea son"])
def test_details_keys_must_be_lowercase_identifiers(key: str) -> None:
    with pytest.raises(AuditError, match="key"):
        details: dict[str, Any] = {key: "x"}
        audit(A.LOGIN_FAILED, **details)


class _Obj:
    _meta = None

    def __init__(self, pk: str) -> None:
        self.pk = pk


@pytest.mark.parametrize("pk", ["a\x00b", "\ud800"])
def test_object_id_is_validated_too(pk: str) -> None:
    with pytest.raises(AuditError, match="object_id"):
        audit(A.LOGIN_FAILED, obj=_Obj(pk))


def test_every_hashed_text_field_goes_through_the_validator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    checked: list[str] = []
    real = audit_module.check_text

    def spy(value: str, path: str) -> None:
        checked.append(path)
        real(value, path)

    monkeypatch.setattr(audit_module, "check_text", spy)
    audit(A.LOGIN_FAILED, obj=_Obj("x"), reason="y")
    assert {"action", "object_type", "object_id", "details.reason"} <= set(checked)


# --------------------------------------------------------------------------- triggers


def _sql(statement: str, params: list[Any] | None = None) -> None:
    with transaction.atomic(), connection.cursor() as cursor:
        # Deferred foreign-key checks pending in the test transaction would make
        # PostgreSQL refuse TRUNCATE before our trigger is reached.
        cursor.execute("SET CONSTRAINTS ALL IMMEDIATE")
        cursor.execute(statement, params or [])


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE vault_auditevent SET action = 'LOGOUT'",
        "DELETE FROM vault_auditevent",
        "TRUNCATE vault_auditevent",
        "TRUNCATE vault_auditevent CASCADE",
    ],
)
def test_update_delete_truncate_are_refused_by_the_database(statement: str) -> None:
    audit(A.LOGIN_FAILED)
    with pytest.raises(DatabaseError, match="append-only"):
        _sql(statement)
    assert AuditEvent.objects.count() == 1


INSERT = (
    "INSERT INTO vault_auditevent (seq, timestamp, action, object_type, object_id, ip_hash, "
    "details_json, prev_hash, hash) VALUES (%s, now(), 'LOGOUT', '', '', '', '{}', %s, %s)"
)


def test_direct_insert_must_extend_the_chain() -> None:
    first = audit(A.LOGIN_FAILED)
    forged = "f" * 64
    for seq, prev in [(1, GENESIS_HASH), (3, first.hash), (2, "a" * 64), (2, GENESIS_HASH)]:
        with pytest.raises(DatabaseError, match="does not extend the chain"):
            _sql(INSERT, [seq, prev, forged])
    _sql(INSERT, [2, first.hash, forged])  # extends it (hash checked by audit_verify)
    assert AuditEvent.objects.count() == 2


@pytest.mark.parametrize("seq", [0, -1])
def test_seq_starts_at_one(seq: int) -> None:
    with pytest.raises(DatabaseError):
        _sql(INSERT, [seq, GENESIS_HASH, "f" * 64])


# --------------------------------------------------------------------------- concurrency


@pytest.mark.django_db(transaction=True)
def test_concurrent_direct_inserts_never_fork_the_chain() -> None:
    """The INSERT trigger takes the audit-log advisory lock: a second direct INSERT
    waits for the first to commit, then sees it and is refused; it never waits on
    the primary key instead. Mutation target: trigger without the lock."""
    first = audit(A.LOGIN_FAILED)
    inserted, release, b_started = threading.Event(), threading.Event(), threading.Event()
    pids: dict[str, int] = {}
    outcome: dict[str, Any] = {}

    def writer_a() -> None:
        with transaction.atomic(), connection.cursor() as cursor:
            cursor.execute(INSERT, [2, first.hash, "a" * 64])
            inserted.set()
            assert release.wait(20)

    def writer_b() -> None:
        assert inserted.wait(10)
        pids["b"] = backend_pid()
        b_started.set()
        try:
            with transaction.atomic(), connection.cursor() as cursor:
                cursor.execute(INSERT, [2, first.hash, "b" * 64])
        except DatabaseError as exc:
            outcome["b"] = exc

    def control() -> None:
        try:
            assert b_started.wait(10), "writer b never started (did writer a fail?)"
            wait_until_blocked(pids["b"], advisory_namespace=locks.AUDIT_NAMESPACE)
        finally:
            release.set()

    run_threads(writer_a, writer_b, control=control)
    assert "does not extend the chain" in str(outcome["b"])
    assert AuditEvent.objects.filter(prev_hash=first.hash).count() == 1


@pytest.mark.django_db(transaction=True)
def test_concurrent_audits_build_one_contiguous_chain() -> None:
    start = threading.Barrier(4)

    def write() -> None:
        start.wait(10)
        for _ in range(5):
            audit(A.LOGIN_FAILED, reason=str(uuid.uuid4()))

    run_threads(write, write, write, write, control=lambda: None)
    events = list(AuditEvent.objects.order_by("seq"))
    assert [e.seq for e in events] == list(range(1, 21))
    for previous, event in itertools.pairwise(events):
        assert event.prev_hash == previous.hash


def test_trigger_takes_the_same_lock_as_audit() -> None:
    from django.db import connection as conn

    with conn.cursor() as cursor:
        cursor.execute("SELECT prosrc FROM pg_proc WHERE proname = 'vault_auditevent_check_link'")
        (source,) = cursor.fetchone()
    expected = f"pg_advisory_xact_lock({locks.AUDIT_NAMESPACE}, {locks.AUDIT_CHAIN_KEY})"
    assert expected in source
