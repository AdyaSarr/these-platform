"""The ONLY module of the project that imports cryptographic libraries (CLAUDE.md rule 1).

Thin wrappers around complete primitives of ``cryptography`` (pyca), with fixed
parameters; nothing is assembled here.

- tokens: ``secrets.token_urlsafe(32)``, SHA-256, ``hmac.compare_digest``;
- sub-keys: HKDF-SHA256 from MASTER_KEY (RFC 5869), keyed pseudonyms (HMAC-SHA256);
- AEAD: AES-256-GCM, fresh random 96-bit nonce per call (NIST SP 800-38D);
- signatures: Ed25519 (RFC 8032), for audit checkpoints.

``cryptography`` exposes HKDF and AESGCM only under ``hazmat``; each is a whole,
standard construction called as is. Byte layouts live in formats.py.

Functions handling key material are wrapped in ``@sensitive_variables()``:
Django then replaces every local variable of their frames (and of the library
frames below them) by stars in error reports. Django applies it only when
DEBUG is False.
"""

import base64
import binascii
import hashlib
import hmac
import re
import secrets

from cryptography.exceptions import InvalidSignature, InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.views.decorators.debug import sensitive_variables

from .errors import INTEGRITY_MESSAGE, VaultIntegrityError

TOKEN_BYTES = 32
MASTER_KEY_BYTES = 32
SUBKEY_BYTES = 32
KEY_BYTES = 32  # AES-256 keys: DEKs and KEKs
NONCE_BYTES = 12  # 96 bits, GCM's native IV length
TAG_BYTES = 16
# Versioned: a new derivation gets a new label, the old one stays reproducible.
# Every HKDF label of the project is a *_INFO constant here (or built by kek_info);
# a test checks that they are pairwise distinct and that no other label is used.
PSEUDONYM_INFO = b"audit-pseudonym-v1"
CHECKPOINT_INFO = b"audit-checkpoint-ed25519-v1"
KEK_INFO_PREFIX = b"vault-kek-v1|"
_PURPOSE = re.compile(r"[a-z][a-z0-9_]{0,31}")
_KEK_LABEL = re.compile(r"[A-Z][A-Z_]{0,31}")


# --------------------------------------------------------------------------- tokens


def new_token() -> str:
    """32 bytes from the OS CSPRNG, base64url without padding (43 characters)."""
    return secrets.token_urlsafe(TOKEN_BYTES)


def hash_token(token: str) -> str:
    """SHA-256 of the token, hex. Enough for 256-bit random tokens (no salt or KDF needed)."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def tokens_match(token: str, stored_hash: str) -> bool:
    """Constant-time check of ``token`` against a stored ``hash_token`` digest."""
    return hmac.compare_digest(hash_token(token), stored_hash)


def sha256(data: bytes) -> bytes:
    return hashlib.sha256(data).digest()


def digests_equal(a: bytes, b: bytes) -> bool:
    """Constant-time equality of two digests."""
    return hmac.compare_digest(a, b)


# --------------------------------------------------------------------------- sub-keys


def _master_key() -> bytes:
    try:
        key = base64.b64decode(settings.MASTER_KEY, validate=True)
    except (AttributeError, binascii.Error, ValueError, TypeError):  # AttributeError: unset
        key = b""
    if len(key) != MASTER_KEY_BYTES:
        # Never echo the value: only say what is expected.
        raise ImproperlyConfigured("MASTER_KEY must be 32 random bytes encoded in base64.")
    return key


# Not cached: a cache would keep MASTER_KEY (as a dict key) and every sub-key in a
# global object, outside @sensitive_variables, to save a few microseconds.
@sensitive_variables()
def _hkdf(master: bytes, info: bytes) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=SUBKEY_BYTES, salt=None, info=info).derive(master)


@sensitive_variables()
def derive_subkey(info: bytes) -> bytes:
    """HKDF-SHA256(MASTER_KEY, salt=None, info) -> 32 bytes. One ``info`` per usage."""
    if not isinstance(info, bytes) or not info:
        raise ValueError("info must be a non-empty bytes label")
    return _hkdf(_master_key(), info)


def kek_info(label: str, version: int) -> bytes:
    """b"vault-kek-v1|" + label + b"|" + decimal version; unambiguous ("|" is in neither)."""
    if not isinstance(label, str) or not _KEK_LABEL.fullmatch(label):
        raise ValueError("KEK label must be an upper-case domain name")
    if type(version) is not int or not 1 <= version < 2**32:
        raise ValueError("KEK version must be an int in [1, 2**32 - 1]")
    return KEK_INFO_PREFIX + label.encode("ascii") + b"|" + str(version).encode("ascii")


@sensitive_variables()
def derive_kek(label: str, version: int) -> bytes:
    """Development KEK of a key domain (e.g. "CONFIDENTIAL", 1), derived from MASTER_KEY."""
    return derive_subkey(kek_info(label, version))


def pseudonym(value: str, *, purpose: str) -> str:
    """HMAC-SHA256 of ``value`` under a sub-key dedicated to audit pseudonyms, hex.

    Lets the audit log correlate events from one IP address or one attempted
    e-mail without storing it: a plain hash would be reversed by enumeration
    (2^32 IPv4 addresses). ``purpose`` ("ip", "email") is bound into the input,
    so the same string yields unrelated pseudonyms for different purposes.
    Independent of SECRET_KEY: rotating it does not break correlations.
    """
    if not _PURPOSE.fullmatch(purpose):
        raise ValueError("purpose must be a short lowercase label")
    key = derive_subkey(PSEUDONYM_INFO)
    message = purpose.encode("ascii") + b"\x00" + value.encode("utf-8")
    return hmac.new(key, message, hashlib.sha256).hexdigest()


# --------------------------------------------------------------------------- AEAD


def new_dek() -> bytes:
    """A fresh data-encryption key: 32 bytes from the OS CSPRNG."""
    return secrets.token_bytes(KEY_BYTES)


def _new_nonce() -> bytes:
    return secrets.token_bytes(NONCE_BYTES)


def _check(key: bytes, associated_data: bytes) -> None:
    if not isinstance(key, bytes) or len(key) != KEY_BYTES:
        raise ValueError("AEAD key must be 32 bytes")
    # AESGCM treats None as "no associated data": refuse it, every caller has some.
    if not isinstance(associated_data, bytes) or not associated_data:
        raise ValueError("associated data must be non-empty bytes")


@sensitive_variables()
def _seal(key: bytes, nonce: bytes, plaintext: bytes, associated_data: bytes) -> bytes:
    return nonce + AESGCM(key).encrypt(nonce, plaintext, associated_data)


@sensitive_variables()
def _open(key: bytes, sealed: bytes, associated_data: bytes) -> bytes:
    if len(sealed) < NONCE_BYTES + TAG_BYTES:
        raise VaultIntegrityError(INTEGRITY_MESSAGE)
    try:
        return AESGCM(key).decrypt(sealed[:NONCE_BYTES], sealed[NONCE_BYTES:], associated_data)
    except InvalidTag:
        raise VaultIntegrityError(INTEGRITY_MESSAGE) from None


@sensitive_variables()
def aead_encrypt(key: bytes, plaintext: bytes, associated_data: bytes) -> bytes:
    """AES-256-GCM under a fresh random nonce; returns nonce | ciphertext | tag."""
    _check(key, associated_data)
    return _seal(key, _new_nonce(), plaintext, associated_data)


@sensitive_variables()
def aead_decrypt(key: bytes, sealed: bytes, associated_data: bytes) -> bytes:
    """Inverse of aead_encrypt; any alteration raises VaultIntegrityError (fixed message)."""
    _check(key, associated_data)
    if not isinstance(sealed, bytes):
        raise VaultIntegrityError(INTEGRITY_MESSAGE)
    return _open(key, sealed, associated_data)


# --------------------------------------------------------------------------- Ed25519


@sensitive_variables()
def ed25519_public_key(seed: bytes) -> bytes:
    return Ed25519PrivateKey.from_private_bytes(seed).public_key().public_bytes_raw()


@sensitive_variables()
def ed25519_sign(seed: bytes, message: bytes) -> bytes:
    return Ed25519PrivateKey.from_private_bytes(seed).sign(message)


def ed25519_verify(public_key: bytes, signature: bytes, message: bytes) -> bool:
    try:
        Ed25519PublicKey.from_public_bytes(public_key).verify(signature, message)
    except (InvalidSignature, ValueError):  # ValueError: malformed public key
        return False
    return True
