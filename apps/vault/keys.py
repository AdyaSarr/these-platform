"""Key-encryption keys (KEK): the only way to reach a KEK.

A KEK version is named by a ``kek_id`` "<provider>:<DOMAIN>:<version>", e.g.
"env:CONFIDENTIAL:3". New data is sealed under the current (highest) version of
its domain, with the provider set in VAULT_KEY_PROVIDER; existing data is opened
with the provider named by its kek_id prefix, if it is in VAULT_ENABLED_PROVIDERS.
This module knows nothing about documents: what references a KEK is reported by
the counters registered per domain (register_reference_counter).

Locking (PostgreSQL advisory locks, held until the end of the transaction,
see vault.locks): ``wrap`` takes the domain's lock SHARED, ``new_version`` and
``retire`` take it EXCLUSIVE. A rotation therefore waits for every transaction
that sealed under the old version to commit, so the rows it wrote are visible
to the rotation and to the reference counters; and two rotations of one domain
never overlap. A WAITING exclusive request also queues new shared requests, so
the exclusive lock is requested with a short lock_timeout: a rotation stuck
behind a long transaction gives up (KekLockTimeoutError) instead of freezing every
wrap of the domain; rotate_kek retries with a growing delay.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from types import MappingProxyType
from typing import TYPE_CHECKING, Protocol

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.db import OperationalError, connection, transaction
from django.db.transaction import TransactionManagementError
from django.utils import timezone
from django.views.decorators.debug import sensitive_variables

from . import crypto, locks
from .errors import (
    KekLockTimeoutError,
    KekRetirementError,
    LockOrderError,
    UnknownKekError,
    VaultKeyError,
)
from .formats import DOMAIN_CODES, MAX_VERSION, KeyDomain
from .models import KeyEncryptionKey

if TYPE_CHECKING:
    from apps.accounts.models import User

ReferenceCounter = Callable[[str], int]

# How long new_version and retire wait for the exclusive lock (milliseconds).
EXCLUSIVE_LOCK_TIMEOUT_MS = 5_000
_LOCK_NOT_AVAILABLE = "55P03"  # SQLSTATE raised when lock_timeout expires

_KEK_ID = re.compile(
    r"(?P<provider>[a-z][a-z0-9]{0,15}):(?P<domain>[A-Z][A-Z_]{0,31}):(?P<version>[1-9][0-9]{0,9})"
)


# --------------------------------------------------------------------------- kek_id


@dataclass(frozen=True)
class KekRef:
    provider: str
    domain: KeyDomain
    version: int

    @property
    def kek_id(self) -> str:
        return format_kek_id(self.provider, self.domain, self.version)


def format_kek_id(provider: str, domain: KeyDomain, version: int) -> str:
    """Same text as the GeneratedField of KeyEncryptionKey (tested)."""
    return f"{provider}:{domain.value}:{version}"


def parse_kek_id(kek_id: str) -> KekRef:
    """Strict parse: one writing per KEK version (no leading zero, no padding)."""
    match = _KEK_ID.fullmatch(kek_id) if isinstance(kek_id, str) else None
    # isascii(): explicit defence in case [0-9] is ever replaced by \d, which
    # accepts Unicode digits (int() would then read U+FF11, a full-width one, as 1).
    if match is None or not match["version"].isascii():
        raise UnknownKekError(f"malformed kek_id {kek_id!r}")
    version = int(match["version"])
    if match["domain"] not in KeyDomain.__members__ or version > MAX_VERSION:
        raise UnknownKekError(f"unknown kek_id {kek_id!r}")
    return KekRef(match["provider"], KeyDomain(match["domain"]), version)


# --------------------------------------------------------------------------- providers


class KeyProvider(Protocol):
    name: str

    def seal(self, domain: KeyDomain, version: int, data: bytes, ad: bytes) -> bytes: ...

    def open(self, domain: KeyDomain, version: int, sealed: bytes, ad: bytes) -> bytes: ...

    def checkpoint_public_key(self) -> bytes: ...

    def sign_checkpoint(self, message: bytes) -> bytes: ...


class EnvKeyProvider:
    """Development: KEKs derived from MASTER_KEY by HKDF; AEAD done here.

    The production provider (OpenBao transit) does the same operations inside
    OpenBao, the KEK never leaving it: see docs/SECURITY.md.
    """

    name = "env"

    @sensitive_variables()
    def seal(self, domain: KeyDomain, version: int, data: bytes, ad: bytes) -> bytes:
        return crypto.aead_encrypt(crypto.derive_kek(domain.value, version), data, ad)

    @sensitive_variables()
    def open(self, domain: KeyDomain, version: int, sealed: bytes, ad: bytes) -> bytes:
        return crypto.aead_decrypt(crypto.derive_kek(domain.value, version), sealed, ad)

    @sensitive_variables()
    def checkpoint_public_key(self) -> bytes:
        return crypto.ed25519_public_key(crypto.derive_subkey(crypto.CHECKPOINT_INFO))

    @sensitive_variables()
    def sign_checkpoint(self, message: bytes) -> bytes:
        return crypto.ed25519_sign(crypto.derive_subkey(crypto.CHECKPOINT_INFO), message)


PROVIDERS: Mapping[str, KeyProvider] = MappingProxyType({"env": EnvKeyProvider()})


def check_settings() -> None:
    """Called at start-up (VaultConfig.ready): unknown providers stop the server."""
    enabled = settings.VAULT_ENABLED_PROVIDERS
    if not isinstance(enabled, frozenset) or not enabled:
        raise ImproperlyConfigured("VAULT_ENABLED_PROVIDERS must be a non-empty frozenset.")
    unknown = sorted(enabled - PROVIDERS.keys())
    if unknown:
        raise ImproperlyConfigured(f"Unknown key providers in VAULT_ENABLED_PROVIDERS: {unknown}")
    if settings.VAULT_KEY_PROVIDER not in enabled:
        raise ImproperlyConfigured("VAULT_KEY_PROVIDER must be one of VAULT_ENABLED_PROVIDERS.")


def _enabled_provider(name: str) -> KeyProvider:
    if name not in settings.VAULT_ENABLED_PROVIDERS or name not in PROVIDERS:
        raise UnknownKekError(f"key provider {name!r} is not enabled")
    return PROVIDERS[name]


# --------------------------------------------------------------------------- locks


def _lock(domain: KeyDomain, *, exclusive: bool) -> None:
    """Transaction-scoped advisory lock on a key domain (vault.locks for the order)."""
    if not connection.in_atomic_block:
        # Outside a transaction the lock would be released at once.
        raise TransactionManagementError("vault.keys must run inside transaction.atomic().")
    key = DOMAIN_CODES[domain]
    if locks.held(locks.AUDIT_NAMESPACE, locks.AUDIT_CHAIN_KEY):
        # Re-taking a lock this transaction already holds never waits; anything
        # else could wait while holding the audit-log lock: refused.
        held = locks.held(locks.KEK_NAMESPACE, key)
        if not (locks.EXCLUSIVE in held or (not exclusive and locks.SHARED in held)):
            raise LockOrderError(
                "KEK lock requested after audit() in the same transaction: "
                "audit() must be the last call of a transaction."
            )
    if not exclusive:
        locks.acquire(locks.KEK_NAMESPACE, key, exclusive=False)
        return
    with connection.cursor() as cursor:
        # SET LOCAL for this one statement, then back to the caller's value
        # (a rolled-back savepoint also undoes it).
        cursor.execute("SELECT current_setting('lock_timeout')")
        (previous,) = cursor.fetchone()
        cursor.execute(
            "SELECT set_config('lock_timeout', %s, true)", [f"{EXCLUSIVE_LOCK_TIMEOUT_MS}ms"]
        )
        locks.acquire(locks.KEK_NAMESPACE, key, exclusive=True)
        cursor.execute("SELECT set_config('lock_timeout', %s, true)", [previous])


def _is_lock_timeout(exc: OperationalError) -> bool:
    return getattr(exc.__cause__, "sqlstate", None) == _LOCK_NOT_AVAILABLE


@contextmanager
def _lock_timeout_as_error(domain: KeyDomain) -> Iterator[None]:
    """lock_timeout expiry (SQLSTATE 55P03) -> KekLockTimeoutError, for rotate_kek to retry."""
    try:
        yield
    except OperationalError as exc:
        if _is_lock_timeout(exc):
            raise KekLockTimeoutError(f"{domain.value}: KEK lock busy, retry later") from exc
        raise


def _check_domain(domain: KeyDomain) -> None:
    if not isinstance(domain, KeyDomain):
        raise ValueError("domain must be a KeyDomain member")


# --------------------------------------------------------------------------- registry


def _current_row(domain: KeyDomain) -> KeyEncryptionKey | None:
    # retire() never retires the highest version, so this is also the highest one.
    return (
        KeyEncryptionKey.objects.filter(domain=domain, retired_at__isnull=True)
        .order_by("-version")
        .first()
    )


def _create(domain: KeyDomain, version: int, actor: User | None) -> KeyEncryptionKey:
    from .services import AuditAction, audit  # services will import keys: no cycle

    row = KeyEncryptionKey.objects.create(
        provider=settings.VAULT_KEY_PROVIDER, domain=domain, version=version, created_by=actor
    )
    row.refresh_from_db(fields=["kek_id"])
    audit(AuditAction.KEK_CREATED, actor=actor, obj=row, kek_id=row.kek_id)
    return row


def current_kek(domain: KeyDomain) -> str:
    """kek_id of the domain's current version. Never creates one (see init_domains).

    Must run inside transaction.atomic(); takes the domain's lock SHARED.
    """
    _check_domain(domain)
    _lock(domain, exclusive=False)
    row = _current_row(domain)
    if row is None:
        raise VaultKeyError(f"{domain.value}: no KEK version: run `manage.py vault_init`")
    return str(row.kek_id)


def missing_domains() -> list[KeyDomain]:
    """Key domains without any KEK version (`manage.py vault_init` not run)."""
    present = set(KeyEncryptionKey.objects.values_list("domain", flat=True).distinct())
    return [domain for domain in KeyDomain if domain.value not in present]


def init_domains(*, actor: User | None = None) -> list[str]:
    """Create version 1 of every domain that has none (manage.py vault_init).

    Idempotent. One transaction: every domain's exclusive lock first (in code
    order), then the rows, then the audit events last, so the lock order holds
    and two concurrent runs create each version once. Run outside business
    transactions: creations are never audited in the middle of a store().
    """
    from .services import AuditAction, audit

    domains = sorted(KeyDomain, key=DOMAIN_CODES.__getitem__)
    with _lock_timeout_as_error(domains[0]), transaction.atomic():
        for domain in domains:
            _lock(domain, exclusive=True)
        rows = [
            KeyEncryptionKey.objects.create(
                provider=settings.VAULT_KEY_PROVIDER, domain=domain, version=1, created_by=actor
            )
            for domain in domains
            if not KeyEncryptionKey.objects.filter(domain=domain).exists()
        ]
        for row in rows:
            row.refresh_from_db(fields=["kek_id"])
            audit(AuditAction.KEK_CREATED, actor=actor, obj=row, kek_id=row.kek_id)
    return [str(row.kek_id) for row in rows]


@sensitive_variables()
def wrap(domain: KeyDomain, data: bytes, ad_for: Callable[[str], bytes]) -> tuple[str, bytes]:
    """Seal ``data`` under the domain's current KEK; returns (kek_id, sealed).

    ``ad_for(kek_id)`` builds the associated data once the KEK is chosen, so it
    always names the KEK actually used. Must run inside the transaction that
    stores the result: the shared lock lasts until it commits.
    """
    kek_id = current_kek(domain)
    ref = parse_kek_id(kek_id)
    return kek_id, _enabled_provider(ref.provider).seal(
        ref.domain, ref.version, data, ad_for(kek_id)
    )


@sensitive_variables()
def unwrap(kek_id: str, sealed: bytes, ad: bytes) -> bytes:
    """Open data sealed under ``kek_id``; unknown, retired or disabled -> UnknownKekError."""
    ref = parse_kek_id(kek_id)
    provider = _enabled_provider(ref.provider)
    if not KeyEncryptionKey.objects.filter(kek_id=kek_id, retired_at__isnull=True).exists():
        raise UnknownKekError(f"unknown or retired kek_id {kek_id!r}")
    return provider.open(ref.domain, ref.version, sealed, ad)


def new_version(domain: KeyDomain, *, actor: User | None = None) -> str:
    """Create the next KEK version of ``domain`` (it becomes current); returns its kek_id.

    Takes the domain's lock EXCLUSIVE: waits for every transaction that is
    sealing under the previous version, and for any other rotation.
    """
    _check_domain(domain)
    with _lock_timeout_as_error(domain), transaction.atomic():
        _lock(domain, exclusive=True)
        last = KeyEncryptionKey.objects.filter(domain=domain).order_by("-version").first()
        version = 1 if last is None else last.version + 1
        if version > MAX_VERSION:
            raise VaultKeyError(f"{domain.value}: no KEK version left")
        return str(_create(domain, version, actor).kek_id)


# --------------------------------------------------------------------------- retirement

_REFERENCE_COUNTERS: dict[KeyDomain, list[ReferenceCounter]] = {}


def register_reference_counter(domain: KeyDomain, counter: ReferenceCounter) -> None:
    """Declare a function counting the stored rows that reference a kek_id of ``domain``.

    Every module that stores data sealed under a domain registers one (in its
    AppConfig.ready); a domain with no counter can never retire a version.
    """
    _check_domain(domain)
    registered = _REFERENCE_COUNTERS.setdefault(domain, [])
    if counter not in registered:  # ready() may run more than once
        registered.append(counter)


def retire(kek_id: str, *, actor: User | None = None) -> None:
    """Retire a KEK version. Refused while it is current, while the NEXT version is
    younger than VAULT_RETIRE_GRACE, while it is referenced, or if uncountable.

    The grace period covers data sealed under this version but stored outside
    the shared lock: encrypt() called outside a transaction (allauth), and the
    window of open() between reading a row and unwrapping its DEK.
    The provider need not be enabled (an old provider's versions are retired
    before it is removed from VAULT_ENABLED_PROVIDERS).
    """
    ref = parse_kek_id(kek_id)
    with _lock_timeout_as_error(ref.domain):
        _retire(ref, kek_id, actor)


def _retire(ref: KekRef, kek_id: str, actor: User | None) -> None:
    from .services import AuditAction, audit

    with transaction.atomic():
        _lock(ref.domain, exclusive=True)
        row = KeyEncryptionKey.objects.filter(kek_id=kek_id, retired_at__isnull=True).first()
        if row is None:
            raise UnknownKekError(f"unknown or already retired kek_id {kek_id!r}")
        current = _current_row(ref.domain)
        if current is not None and current.pk == row.pk:
            raise KekRetirementError(f"{kek_id} is the current version of its domain")
        following = (
            KeyEncryptionKey.objects.filter(domain=ref.domain, version__gt=row.version)
            .order_by("version")
            .first()
        )
        grace = settings.VAULT_RETIRE_GRACE
        if following is None or following.created_at > timezone.now() - grace:
            raise KekRetirementError(
                f"{kek_id}: grace period not elapsed: the next version must exist for {grace}"
            )
        counters = _REFERENCE_COUNTERS.get(ref.domain)
        if not counters:
            raise KekRetirementError(f"no reference counter registered for {ref.domain.value}")
        references = sum(counter(kek_id) for counter in counters)
        if references:
            raise KekRetirementError(f"{kek_id} is still referenced by {references} row(s)")
        row.retired_at = timezone.now()
        row.save(update_fields=["retired_at", "updated_at"])
        audit(AuditAction.KEK_RETIRED, actor=actor, obj=row, kek_id=kek_id)


# --------------------------------------------------------------------------- checkpoints


def checkpoint_public_key() -> bytes:
    """Public key of the audit checkpoints (exported once, pinned outside the platform)."""
    return _enabled_provider(settings.VAULT_KEY_PROVIDER).checkpoint_public_key()


@sensitive_variables()
def sign_checkpoint(message: bytes) -> bytes:
    return _enabled_provider(settings.VAULT_KEY_PROVIDER).sign_checkpoint(message)
