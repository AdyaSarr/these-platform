"""vault.keys: KEK registry, providers, rotation primitives and their locking."""

import threading
import time
from collections.abc import Callable, Generator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from django.apps import apps
from django.core.exceptions import ImproperlyConfigured
from django.db import DatabaseError, connection, transaction
from django.db.transaction import TransactionManagementError

from apps.accounts.models import Organization
from apps.vault import crypto, keys, locks
from apps.vault.errors import (
    KekLockTimeoutError,
    KekRetirementError,
    LockOrderError,
    UnknownKekError,
    VaultIntegrityError,
    VaultKeyError,
)
from apps.vault.formats import KeyDomain
from apps.vault.models import AuditEvent, KeyEncryptionKey
from apps.vault.services import AuditAction, audit

# This module manages KEK rows itself (no automatic version 1, see conftest.py).
pytestmark = pytest.mark.without_vault_keys

C = KeyDomain.CONFIDENTIAL
DEK = b"\x42" * 32


def _ad(kek_id: str) -> bytes:
    return b"test-ad|" + kek_id.encode("ascii")


@pytest.fixture
def counters() -> Generator[None]:
    """Isolate the reference-counter registry."""
    saved = {d: list(c) for d, c in keys._REFERENCE_COUNTERS.items()}
    keys._REFERENCE_COUNTERS.clear()
    yield
    keys._REFERENCE_COUNTERS.clear()
    keys._REFERENCE_COUNTERS.update(saved)


@pytest.fixture(autouse=True)
def _no_retire_grace(settings: Any) -> None:
    """Retire right after new_version(); the grace period has its own tests."""
    settings.VAULT_RETIRE_GRACE = timedelta(0)


def _create_v1(domain: KeyDomain = C) -> str:
    """Version 1 as ``vault_init`` creates it, without its audit event."""
    return str(KeyEncryptionKey.objects.create(provider="env", domain=domain, version=1).kek_id)


@pytest.fixture
def v1() -> str:
    """Version 1 created directly, without the (audited) bootstrap of wrap().

    In one test transaction, a bootstrap's audit() would come before the
    new_version() of the test, which the lock order forbids; in production
    rotate_kek runs new_version() in its own transaction.
    """
    return str(KeyEncryptionKey.objects.create(provider="env", domain=C, version=1).kek_id)


def _wrap(domain: KeyDomain = C, data: bytes = DEK) -> tuple[str, bytes]:
    with transaction.atomic():
        return keys.wrap(domain, data, _ad)


def _unwrap(kek_id: str, sealed: bytes) -> bytes:
    return keys.unwrap(kek_id, sealed, _ad(kek_id))


# --------------------------------------------------------------------------- kek_id


@pytest.mark.parametrize(
    ("kek_id", "parts"),
    [
        ("env:CONFIDENTIAL:1", ("env", C, 1)),
        ("env:MFA_SECRETS:42", ("env", KeyDomain.MFA_SECRETS, 42)),
        ("bao:PUBLIC:4294967295", ("bao", KeyDomain.PUBLIC, 2**32 - 1)),
    ],
)
def test_parse_kek_id(kek_id: str, parts: tuple[str, KeyDomain, int]) -> None:
    ref = keys.parse_kek_id(kek_id)
    assert (ref.provider, ref.domain, ref.version) == parts
    assert ref.kek_id == kek_id


@pytest.mark.parametrize(
    "kek_id",
    [
        "",
        "env:CONFIDENTIAL:0",
        "env:CONFIDENTIAL:01",  # non-canonical: one writing per version
        "env:CONFIDENTIAL:+1",
        "env:CONFIDENTIAL:4294967296",
        "env:SECRET:1",  # unknown domain
        "ENV:CONFIDENTIAL:1",
        "env:confidential:1",
        "env:CONFIDENTIAL",
        "env:CONFIDENTIAL:1:x",
        " env:CONFIDENTIAL:1",
        "env:CONFIDENTIAL:1\n",
        "env:CONFIDENTIAL:１",  # noqa: RUF001 - full-width digit
    ],
)
def test_malformed_kek_id_is_unknown(kek_id: str) -> None:
    with pytest.raises(UnknownKekError):
        keys.parse_kek_id(kek_id)


# --------------------------------------------------------------------------- model


def test_model_holds_no_key_material() -> None:
    names = {f.name for f in KeyEncryptionKey._meta.get_fields()}
    assert names == {
        "id",
        "provider",
        "domain",
        "version",
        "kek_id",
        "retired_at",
        "created_at",
        "updated_at",
        "created_by",
    }


@pytest.mark.django_db
def test_kek_id_is_computed_by_postgresql() -> None:
    row = KeyEncryptionKey.objects.create(provider="env", domain=C, version=7)
    row.refresh_from_db()
    assert row.kek_id == "env:CONFIDENTIAL:7" == keys.format_kek_id("env", C, 7)
    field = KeyEncryptionKey._meta.get_field("kek_id")
    assert field.generated
    assert field.db_persist  # type: ignore[attr-defined]


@pytest.mark.django_db
def test_kek_id_cannot_be_written_through_the_orm() -> None:
    """Django silently drops generated fields from INSERT and UPDATE."""
    KeyEncryptionKey.objects.bulk_create(
        [KeyEncryptionKey(provider="env", domain=C, version=1, kek_id="env:CONFIDENTIAL:9")]
    )
    KeyEncryptionKey.objects.update(kek_id="env:PUBLIC:1")
    assert list(KeyEncryptionKey.objects.values_list("kek_id", flat=True)) == ["env:CONFIDENTIAL:1"]


@pytest.mark.django_db
@pytest.mark.parametrize(
    "sql",
    [
        "INSERT INTO vault_keyencryptionkey (id, provider, domain, version, kek_id, "
        "created_at, updated_at) VALUES (gen_random_uuid(), 'env', 'CONFIDENTIAL', 2, "
        "'env:PUBLIC:1', now(), now())",
        "UPDATE vault_keyencryptionkey SET kek_id = 'env:PUBLIC:1'",
    ],
    ids=["insert", "update"],
)
def test_kek_id_cannot_be_written_in_sql(sql: str) -> None:
    KeyEncryptionKey.objects.create(provider="env", domain=C, version=1)
    with pytest.raises(DatabaseError), transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(sql)
    assert KeyEncryptionKey.objects.get().kek_id == "env:CONFIDENTIAL:1"


@pytest.mark.django_db
@pytest.mark.parametrize(
    "fields",
    [
        {"provider": "env", "domain": "SECRET", "version": 1},
        {"provider": "env", "domain": C, "version": 0},
        {"provider": "Env", "domain": C, "version": 1},
        {"provider": "env:x", "domain": C, "version": 1},
    ],
)
def test_database_refuses_invalid_rows(fields: dict[str, Any]) -> None:
    with pytest.raises(DatabaseError), transaction.atomic():
        KeyEncryptionKey.objects.create(**fields)


@pytest.mark.django_db
def test_one_row_per_domain_and_version() -> None:
    KeyEncryptionKey.objects.create(provider="env", domain=C, version=1)
    with pytest.raises(DatabaseError), transaction.atomic():
        KeyEncryptionKey.objects.create(provider="bao", domain=C, version=1)


# --------------------------------------------------------------------------- wrap / unwrap


@pytest.mark.django_db
def test_wrap_round_trips(v1: str) -> None:
    kek_id, sealed = _wrap()
    assert kek_id == v1 == "env:CONFIDENTIAL:1"
    assert DEK not in sealed
    assert _unwrap(kek_id, sealed) == DEK


@pytest.mark.django_db
def test_current_kek_never_creates_a_version() -> None:
    with pytest.raises(VaultKeyError, match="vault_init"), transaction.atomic():
        keys.current_kek(C)
    assert not KeyEncryptionKey.objects.exists()


@pytest.mark.django_db
def test_init_domains_creates_version_1_of_every_domain_once() -> None:
    assert sorted(keys.init_domains()) == sorted(f"env:{d.value}:1" for d in KeyDomain)
    assert keys.init_domains() == []  # idempotent
    assert KeyEncryptionKey.objects.count() == len(KeyDomain)
    assert AuditEvent.objects.filter(action="KEK_CREATED").count() == len(KeyDomain)


@pytest.mark.django_db
def test_init_domains_leaves_rotated_domains_alone(v1: str) -> None:
    KeyEncryptionKey.objects.create(provider="env", domain=C, version=2)
    created = keys.init_domains()
    assert f"env:{C.value}:1" not in created
    assert KeyEncryptionKey.objects.filter(domain=C).count() == 2


@pytest.mark.django_db
def test_vault_init_creates_the_keys() -> None:
    from io import StringIO

    from django.core.management import call_command

    out = StringIO()
    call_command("vault_init", stdout=out)
    assert KeyEncryptionKey.objects.count() == len(KeyDomain)
    assert "env:CONFIDENTIAL:1" in out.getvalue()


@pytest.mark.django_db
def test_ad_builder_receives_the_kek_id_actually_used(v1: str) -> None:
    seen: list[str] = []

    def ad_for(kek_id: str) -> bytes:
        seen.append(kek_id)
        return _ad(kek_id)

    with transaction.atomic():
        kek_id, sealed = keys.wrap(C, DEK, ad_for)
    assert seen == [kek_id]
    # The KEK really is the development KEK of that version, under that AD.
    assert crypto.aead_decrypt(crypto.derive_kek("CONFIDENTIAL", 1), sealed, _ad(kek_id)) == DEK


@pytest.mark.django_db
def test_ad_with_another_kek_id_fails_integrity(v1: str) -> None:
    kek_id, sealed = _wrap()
    with pytest.raises(VaultIntegrityError):
        keys.unwrap(kek_id, sealed, _ad("env:CONFIDENTIAL:2"))


@pytest.mark.django_db
def test_domains_have_independent_keks() -> None:
    _create_v1(KeyDomain.PUBLIC)
    kek_id, sealed = _wrap(KeyDomain.PUBLIC)
    assert kek_id == "env:PUBLIC:1"
    with pytest.raises(VaultIntegrityError):
        crypto.aead_decrypt(crypto.derive_kek("CONFIDENTIAL", 1), sealed, _ad(kek_id))


def test_wrap_outside_a_transaction_is_refused() -> None:
    """The shared lock must last until the row referencing the KEK is committed."""
    with pytest.raises(TransactionManagementError):
        keys.wrap(C, DEK, _ad)


@pytest.mark.django_db
@pytest.mark.parametrize("domain", ["CONFIDENTIAL", None, 4])
def test_wrap_requires_a_key_domain(domain: Any) -> None:
    with pytest.raises(ValueError), transaction.atomic():
        keys.wrap(domain, DEK, _ad)


@pytest.mark.django_db
def test_new_version_is_used_for_new_wraps_and_old_one_stays_readable(v1: str) -> None:
    old_id, old_sealed = _wrap()
    assert keys.new_version(C) == "env:CONFIDENTIAL:2"
    new_id, new_sealed = _wrap()
    assert new_id == "env:CONFIDENTIAL:2"
    assert _unwrap(old_id, old_sealed) == DEK
    assert _unwrap(new_id, new_sealed) == DEK


@pytest.mark.django_db
def test_unknown_but_well_formed_kek_id_is_refused(v1: str) -> None:
    _wrap()
    with pytest.raises(UnknownKekError):
        _unwrap("env:CONFIDENTIAL:5", b"\x00" * 60)


@pytest.mark.django_db
def test_disabled_provider_is_refused(settings: Any, v1: str) -> None:
    kek_id, sealed = _wrap()
    settings.VAULT_ENABLED_PROVIDERS = frozenset({"bao"})
    with pytest.raises(UnknownKekError):
        _unwrap(kek_id, sealed)


@pytest.mark.django_db
def test_kek_id_of_an_unknown_provider_is_refused() -> None:
    KeyEncryptionKey.objects.create(provider="bao", domain=C, version=1)
    with pytest.raises(UnknownKekError):
        _unwrap("bao:CONFIDENTIAL:1", b"\x00" * 60)


@pytest.mark.django_db
def test_exhausted_domain_is_a_key_error() -> None:
    KeyEncryptionKey.objects.create(provider="env", domain=C, version=2**32 - 1)
    with pytest.raises(VaultKeyError) as excinfo:
        keys.new_version(C)
    assert not isinstance(excinfo.value, KekRetirementError)


# --------------------------------------------------------------------------- retire


def _referenced_by(kek_ids: set[str]) -> Callable[[str], int]:
    return lambda kek_id: int(kek_id in kek_ids)


@pytest.mark.django_db
def test_retired_kek_is_refused(counters: None, v1: str) -> None:
    keys.register_reference_counter(C, _referenced_by(set()))
    kek_id, sealed = _wrap()
    keys.new_version(C)
    keys.retire(kek_id)
    assert KeyEncryptionKey.objects.get(kek_id=kek_id).retired_at is not None
    with pytest.raises(UnknownKekError):
        _unwrap(kek_id, sealed)
    with pytest.raises(UnknownKekError):  # already retired
        keys.retire(kek_id)
    assert AuditEvent.objects.filter(action="KEK_RETIRED").count() == 1


@pytest.mark.django_db
def test_current_version_cannot_be_retired(counters: None, v1: str) -> None:
    keys.register_reference_counter(C, _referenced_by(set()))
    kek_id, _ = _wrap()
    with pytest.raises(KekRetirementError):
        keys.retire(kek_id)


@pytest.mark.django_db
def test_referenced_version_cannot_be_retired(counters: None, v1: str) -> None:
    kek_id, sealed = _wrap()
    keys.register_reference_counter(C, _referenced_by({kek_id}))
    keys.new_version(C)
    with pytest.raises(KekRetirementError):
        keys.retire(kek_id)
    assert _unwrap(kek_id, sealed) == DEK


@pytest.mark.django_db
def test_counters_of_a_domain_are_summed(counters: None, v1: str) -> None:
    kek_id, _ = _wrap()
    keys.register_reference_counter(C, _referenced_by(set()))
    keys.register_reference_counter(C, _referenced_by({kek_id}))
    keys.new_version(C)
    with pytest.raises(KekRetirementError):
        keys.retire(kek_id)


@pytest.mark.django_db
def test_domain_without_counter_cannot_retire(counters: None, v1: str) -> None:
    """Deny by default: no way to know nothing references the KEK."""
    kek_id, _ = _wrap()
    keys.new_version(C)
    with pytest.raises(KekRetirementError):
        keys.retire(kek_id)


def test_counter_registration_is_validated(counters: None) -> None:
    with pytest.raises(ValueError):
        keys.register_reference_counter("CONFIDENTIAL", _referenced_by(set()))  # type: ignore[arg-type]


# --------------------------------------------------------------------------- settings


@pytest.mark.parametrize(
    "overrides",
    [
        {"VAULT_ENABLED_PROVIDERS": frozenset({"env", "hsm"})},
        {"VAULT_ENABLED_PROVIDERS": frozenset()},
        {"VAULT_ENABLED_PROVIDERS": ["env"]},
        {"VAULT_KEY_PROVIDER": "hsm"},
        {"VAULT_KEY_PROVIDER": "bao", "VAULT_ENABLED_PROVIDERS": frozenset({"env"})},
    ],
)
def test_invalid_provider_settings_stop_start_up(settings: Any, overrides: dict[str, Any]) -> None:
    for name, value in overrides.items():
        setattr(settings, name, value)
    with pytest.raises(ImproperlyConfigured):
        keys.check_settings()
    with pytest.raises(ImproperlyConfigured):
        apps.get_app_config("vault").ready()


def test_default_settings() -> None:
    from django.conf import settings

    assert settings.VAULT_KEY_PROVIDER == "env"
    assert frozenset({"env"}) == settings.VAULT_ENABLED_PROVIDERS
    keys.check_settings()


# --------------------------------------------------------------------------- checkpoint key


def test_checkpoint_key_is_derived_from_its_own_label() -> None:
    seed = crypto.derive_subkey(crypto.CHECKPOINT_INFO)
    public = keys.checkpoint_public_key()
    assert public == crypto.ed25519_public_key(seed)
    signature = keys.sign_checkpoint(b"seq=1|hash=00")
    assert crypto.ed25519_verify(public, signature, b"seq=1|hash=00")


@pytest.mark.parametrize(
    "func",
    [
        keys.wrap,
        keys.unwrap,
        keys.sign_checkpoint,
        keys.EnvKeyProvider.seal,
        keys.EnvKeyProvider.open,
        keys.EnvKeyProvider.sign_checkpoint,
        keys.EnvKeyProvider.checkpoint_public_key,
    ],
)
def test_key_handling_functions_are_marked_sensitive(func: Any) -> None:
    assert func.__code__.co_name == "sensitive_variables_wrapper"


# --------------------------------------------------------------------------- retire grace

T0 = datetime(2026, 11, 13, 9, 0, tzinfo=UTC)


@pytest.mark.django_db
def test_retire_waits_for_the_grace_period_of_the_next_version(
    counters: None, v1: str, settings: Any, time_machine: Any
) -> None:
    """Covers encrypt() calls made outside a transaction (allauth) and the
    window of open() between reading a row and unwrapping its DEK."""
    settings.VAULT_RETIRE_GRACE = timedelta(minutes=10)
    keys.register_reference_counter(C, _referenced_by(set()))
    time_machine.move_to(T0, tick=False)
    keys.new_version(C)
    time_machine.move_to(T0 + timedelta(minutes=9, seconds=59), tick=False)
    with pytest.raises(KekRetirementError, match="grace"):
        keys.retire(v1)
    assert KeyEncryptionKey.objects.get(kek_id=v1).retired_at is None
    time_machine.move_to(T0 + timedelta(minutes=10, seconds=1), tick=False)
    keys.retire(v1)
    assert KeyEncryptionKey.objects.get(kek_id=v1).retired_at is not None


@pytest.mark.django_db
def test_grace_is_measured_on_the_next_version_not_the_latest(
    counters: None, v1: str, settings: Any, time_machine: Any
) -> None:
    settings.VAULT_RETIRE_GRACE = timedelta(minutes=10)
    keys.register_reference_counter(C, _referenced_by(set()))
    time_machine.move_to(T0, tick=False)
    keys.new_version(C)  # v2, the version following v1
    time_machine.move_to(T0 + timedelta(hours=1), tick=False)
    keys.new_version(C)  # v3, brand new
    keys.retire(v1)  # v2 is old enough: allowed although v3 is not
    with pytest.raises(KekRetirementError, match="grace"):
        keys.retire("env:CONFIDENTIAL:2")  # v3 is too recent


def test_default_retire_grace_is_10_minutes() -> None:
    from config.settings import base  # this module's fixture sets it to 0

    assert timedelta(minutes=10) == base.VAULT_RETIRE_GRACE


# --------------------------------------------------------------------------- lock order


@pytest.mark.django_db
def test_wrap_after_audit_in_the_same_transaction_is_refused(v1: str) -> None:
    with transaction.atomic():
        audit(AuditAction.LOGIN_FAILED)
        assert locks.held(locks.AUDIT_NAMESPACE, locks.AUDIT_CHAIN_KEY) == {locks.EXCLUSIVE}
        with pytest.raises(LockOrderError):
            keys.wrap(C, DEK, _ad)


@pytest.mark.django_db
def test_new_version_after_audit_in_the_same_transaction_is_refused(v1: str) -> None:
    with transaction.atomic():
        keys.wrap(C, DEK, _ad)  # shared held: upgrading to exclusive could wait
        audit(AuditAction.LOGIN_FAILED)
        with pytest.raises(LockOrderError):
            keys.new_version(C)


@pytest.mark.django_db
def test_wrap_after_audit_in_another_domain_is_refused(v1: str) -> None:
    KeyEncryptionKey.objects.create(provider="env", domain=KeyDomain.PUBLIC, version=1)
    with transaction.atomic():
        keys.wrap(C, DEK, _ad)
        audit(AuditAction.LOGIN_FAILED)
        with pytest.raises(LockOrderError):
            keys.wrap(KeyDomain.PUBLIC, DEK, _ad)


@pytest.mark.django_db
def test_re_taking_a_held_kek_lock_after_audit_is_allowed(v1: str) -> None:
    """Re-taking a lock already held never waits: a transaction that wrapped, then
    audited, may wrap again in the same domain."""
    with transaction.atomic():
        first, _ = keys.wrap(C, DEK, _ad)
        audit(AuditAction.LOGIN_FAILED)
        second, _ = keys.wrap(C, DEK, _ad)
    assert first == second == v1


@pytest.mark.django_db
def test_exclusive_lock_timeout_does_not_leak_into_the_caller(v1: str) -> None:
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute("SET LOCAL lock_timeout = '42s'")
        keys.new_version(C)
        cursor.execute("SELECT current_setting('lock_timeout')")
        assert cursor.fetchone()[0] == "42s"


# --------------------------------------------------------------------------- concurrency
# Real concurrent connections: Django gives each thread its own connection, and
# transactional_db commits for real (no wrapping test transaction). Waits are
# checked in pg_locks (granted = false), not with durations; each thread also
# has a 10 s lock_timeout as a safety net.


def _org_counter(kek_id: str) -> int:
    return Organization.objects.filter(name=f"ref:{kek_id}").count()


def _pid() -> int:
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_backend_pid()")
        return int(cursor.fetchone()[0])


def _wait_until_blocked(pid: int, *, advisory: bool = True, within: float = 5.0) -> None:
    """Poll pg_locks until backend ``pid`` waits for a lock (a KEK lock by default)."""
    sql = "SELECT EXISTS (SELECT 1 FROM pg_locks WHERE pid = %s AND NOT granted"
    params: list[int] = [pid]
    if advisory:
        sql += " AND locktype = 'advisory' AND classid = %s"
        params.append(locks.KEK_NAMESPACE)
    deadline = time.monotonic() + within
    while time.monotonic() < deadline:
        with connection.cursor() as cursor:
            cursor.execute(sql + ")", params)
            if cursor.fetchone()[0]:
                return
        time.sleep(0.01)
    raise AssertionError(f"backend {pid} never waited for a lock")


def _run(*targets: Callable[[], None], control: Callable[[], None]) -> None:
    """Run ``targets`` in threads while ``control`` observes them from this thread.

    ``control`` must release every waiting thread in a ``finally``.
    """
    errors: list[BaseException] = []

    def guarded(target: Callable[[], None]) -> Callable[[], None]:
        def run() -> None:
            try:
                with connection.cursor() as cursor:  # never hang the suite
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


class Pids:
    """Backend pids published by the threads, each with an event."""

    def __init__(self) -> None:
        self._pids: dict[str, int] = {}
        self._events: dict[str, threading.Event] = {}
        self._lock = threading.Lock()

    def _event(self, name: str) -> threading.Event:
        with self._lock:
            return self._events.setdefault(name, threading.Event())

    def publish(self, name: str) -> None:
        self._pids[name] = _pid()
        self._event(name).set()

    def get(self, name: str) -> int:
        assert self._event(name).wait(10), f"{name} never started"
        return self._pids[name]


@pytest.mark.django_db(transaction=True)
@pytest.mark.serial
def test_rotation_waits_for_an_uncommitted_wrap_then_refuses_to_retire(counters: None) -> None:
    """An uncommitted wrap under v1 is invisible to other connections: without the
    shared lock, rotation would create v2 and retire v1 while a row under v1 is
    about to be committed, leaving it unreadable."""
    v1 = _create_v1()
    keys.register_reference_counter(C, _org_counter)
    wrapped, release = threading.Event(), threading.Event()
    pids = Pids()
    outcome: dict[str, Any] = {}

    def business_transaction() -> None:
        with transaction.atomic():
            kek_id, _ = keys.wrap(C, DEK, _ad)
            outcome["wrapped_under"] = kek_id
            Organization.objects.create(name=f"ref:{kek_id}", kind="EXTERNAL")
            wrapped.set()
            assert release.wait(20)

    def rotation() -> None:
        assert wrapped.wait(10)
        pids.publish("rotation")
        keys.new_version(C)
        try:
            keys.retire(v1)
        except KekRetirementError as exc:
            outcome["refused"] = exc

    def control() -> None:
        try:
            _wait_until_blocked(pids.get("rotation"))  # new_version waits for the commit
        finally:
            release.set()

    _run(business_transaction, rotation, control=control)
    assert outcome["wrapped_under"] == v1
    assert "refused" in outcome  # then saw the committed row referencing v1
    assert KeyEncryptionKey.objects.get(kek_id=v1).retired_at is None


@pytest.mark.django_db(transaction=True)
@pytest.mark.serial
def test_retire_waits_for_transactions_holding_the_shared_lock(counters: None) -> None:
    v1 = _create_v1()
    keys.new_version(C)
    keys.register_reference_counter(C, _referenced_by(set()))
    wrapped, release = threading.Event(), threading.Event()
    pids = Pids()

    def business_transaction() -> None:
        with transaction.atomic():
            keys.wrap(C, DEK, _ad)  # under v2, but holds the domain's shared lock
            wrapped.set()
            assert release.wait(20)

    def retirement() -> None:
        assert wrapped.wait(10)
        pids.publish("retire")
        keys.retire(v1)

    def control() -> None:
        try:
            _wait_until_blocked(pids.get("retire"))
        finally:
            release.set()

    _run(business_transaction, retirement, control=control)
    assert KeyEncryptionKey.objects.get(kek_id=v1).retired_at is not None


ROTATION_GIVE_UP_BOUND = 5.0  # > the test's 1.5 s exclusive timeout, < the 10 s safety net


@pytest.mark.django_db(transaction=True)
@pytest.mark.serial
def test_waiting_rotation_gives_up_and_does_not_freeze_wraps(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A WAITING exclusive request queues new shared ones. new_version waits at most
    EXCLUSIVE_LOCK_TIMEOUT_MS behind a long transaction, then fails; a wrap that
    queued behind it then goes through while the long transaction is still open."""
    monkeypatch.setattr(keys, "EXCLUSIVE_LOCK_TIMEOUT_MS", 1_500)
    _create_v1()
    long_held, start_wrap, release = threading.Event(), threading.Event(), threading.Event()
    wrap_done = threading.Event()
    pids = Pids()
    outcome: dict[str, Any] = {}

    def long_transaction() -> None:
        with transaction.atomic():
            keys.wrap(C, DEK, _ad)
            long_held.set()
            assert release.wait(30)

    def rotation() -> None:
        assert long_held.wait(10)
        pids.publish("rotation")
        try:
            keys.new_version(C)
        except KekLockTimeoutError as exc:
            outcome["timeout"] = exc

    def late_wrap() -> None:
        assert start_wrap.wait(10)
        pids.publish("wrap")
        with transaction.atomic():
            keys.wrap(C, DEK, _ad)
        wrap_done.set()

    def control() -> None:
        try:
            _wait_until_blocked(pids.get("rotation"))
            start_wrap.set()
            _wait_until_blocked(pids.get("wrap"))  # queued behind the waiting rotation
            # Released once the rotation gives up (1.5 s), NOT by the long transaction
            # nor by the 10 s safety net: the bound must lie strictly between them.
            assert wrap_done.wait(ROTATION_GIVE_UP_BOUND), "wrap blocked beyond the timeout"
        finally:
            release.set()

    _run(long_transaction, rotation, late_wrap, control=control)
    assert isinstance(outcome.get("timeout"), KekLockTimeoutError)
    assert KeyEncryptionKey.objects.filter(domain=C).count() == 1  # no v2


@pytest.mark.django_db(transaction=True)
@pytest.mark.serial
def test_concurrent_rotations_are_serialized() -> None:
    _create_v1()
    results: list[str] = []
    start = threading.Barrier(2)

    def rotate() -> None:
        start.wait(10)
        results.append(keys.new_version(C))

    _run(rotate, rotate, control=lambda: None)
    assert sorted(results) == ["env:CONFIDENTIAL:2", "env:CONFIDENTIAL:3"]


@pytest.mark.django_db(transaction=True)
@pytest.mark.serial
def test_concurrent_vault_init_creates_each_version_once() -> None:
    start = threading.Barrier(2)

    def init() -> None:
        start.wait(10)
        keys.init_domains()

    _run(init, init, control=lambda: None)
    assert KeyEncryptionKey.objects.count() == len(KeyDomain)
    assert set(KeyEncryptionKey.objects.values_list("version", flat=True)) == {1}
