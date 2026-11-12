"""The ONLY module of the project that imports cryptographic libraries (CLAUDE.md rule 1).

M1 scope: random tokens, their SHA-256 digest, constant-time comparison, sub-key
derivation from MASTER_KEY (HKDF-SHA256) and keyed pseudonyms. M2 adds AEAD and
envelope encryption here.

``cryptography`` exposes HKDF only under ``hazmat``; we call that single,
complete primitive with fixed parameters and assemble nothing ourselves.
"""

import base64
import binascii
import functools
import hashlib
import hmac
import re
import secrets

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

TOKEN_BYTES = 32
MASTER_KEY_BYTES = 32
SUBKEY_BYTES = 32
# Versioned: a new derivation gets a new label, the old one stays reproducible.
PSEUDONYM_INFO = b"audit-pseudonym-v1"
_PURPOSE = re.compile(r"[a-z][a-z0-9_]{0,31}")


def new_token() -> str:
    """32 bytes from the OS CSPRNG, base64url without padding (43 characters)."""
    return secrets.token_urlsafe(TOKEN_BYTES)


def hash_token(token: str) -> str:
    """SHA-256 of the token, hex. Enough for 256-bit random tokens (no salt or KDF needed)."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def tokens_match(token: str, stored_hash: str) -> bool:
    """Constant-time check of ``token`` against a stored ``hash_token`` digest."""
    return hmac.compare_digest(hash_token(token), stored_hash)


def _master_key() -> bytes:
    try:
        key = base64.b64decode(settings.MASTER_KEY, validate=True)
    except (binascii.Error, ValueError, TypeError):
        key = b""
    if len(key) != MASTER_KEY_BYTES:
        # Never echo the value: only say what is expected.
        raise ImproperlyConfigured("MASTER_KEY must be 32 random bytes encoded in base64.")
    return key


@functools.lru_cache(maxsize=8)
def _hkdf(master: bytes, info: bytes) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=SUBKEY_BYTES, salt=None, info=info).derive(master)


def derive_subkey(info: bytes) -> bytes:
    """HKDF-SHA256(MASTER_KEY, salt=None, info) -> 32 bytes. One ``info`` per usage."""
    if not isinstance(info, bytes) or not info:
        raise ValueError("info must be a non-empty bytes label")
    return _hkdf(_master_key(), info)


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
