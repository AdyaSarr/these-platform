"""Byte layouts of vault format v1: associated data, blob, encrypted MFA secret.

Pure encoding: no cryptography here (CLAUDE.md rule 1, see crypto.py).

Every layout is injective: fixed-length fields first, then at most one
variable field, prefixed by its length, at the very end. The first byte is the
format version and the second a context byte, so that encodings made for
different purposes never coincide. Changing a layout means a new format byte;
data written under v1 must stay decodable.

    content AD   = 0x01 | 0x01 | thesis_id(16) | document_id(16) | version(4, BE) | level(1)
    DEK-wrap AD  = 0x01 | 0x02 | thesis_id | document_id | version | level | len(1) | kek_id
    MFA AD       = 0x01 | 0x03 | 0x80 | len(1) | kek_id
    blob         = 0x01 | sealed            (sealed = nonce | ciphertext | tag, see crypto)
    sealed MFA   = "vault1:" + base64url(len(1) | kek_id | sealed)

kek_id is deliberately absent from the content AD: rotating a KEK re-wraps the
DEKs only and leaves the blobs untouched (docs/SECURITY.md).
"""

from __future__ import annotations

import base64
import binascii
import re
import uuid
from enum import IntEnum, StrEnum
from types import MappingProxyType
from typing import TYPE_CHECKING

from apps.core.confidentiality import ConfidentialityLevel

from .errors import INTEGRITY_MESSAGE, VaultIntegrityError

if TYPE_CHECKING:
    from collections.abc import Mapping

FORMAT_V1 = 0x01


class Context(IntEnum):
    CONTENT = 0x01
    DEK_WRAP = 0x02
    MFA_SECRET = 0x03


class KeyDomain(StrEnum):
    """What a KEK protects: one per confidentiality level, plus the MFA secrets."""

    PUBLIC = "PUBLIC"
    INTERNAL_LIRMM = "INTERNAL_LIRMM"
    INTERNAL_ST = "INTERNAL_ST"
    CONFIDENTIAL = "CONFIDENTIAL"
    MFA_SECRETS = "MFA_SECRETS"  # pragma: allowlist secret (a domain name)


# FROZEN. These bytes are inside the associated data of every stored ciphertext:
# never change a code, never reuse one, only add new ones.
DOMAIN_CODES: Mapping[KeyDomain, int] = MappingProxyType(
    {
        KeyDomain.PUBLIC: 0x01,
        KeyDomain.INTERNAL_LIRMM: 0x02,
        KeyDomain.INTERNAL_ST: 0x03,
        KeyDomain.CONFIDENTIAL: 0x04,
        KeyDomain.MFA_SECRETS: 0x80,
    }
)

# Explicit, not derived from the member names: renaming a level must not
# silently move its documents to another key domain.
LEVEL_TO_DOMAIN: Mapping[ConfidentialityLevel, KeyDomain] = MappingProxyType(
    {
        ConfidentialityLevel.PUBLIC: KeyDomain.PUBLIC,
        ConfidentialityLevel.INTERNAL_LIRMM: KeyDomain.INTERNAL_LIRMM,
        ConfidentialityLevel.INTERNAL_ST: KeyDomain.INTERNAL_ST,
        ConfidentialityLevel.CONFIDENTIAL: KeyDomain.CONFIDENTIAL,
    }
)

MAX_VERSION = 2**32 - 1
KEK_ID_MAX_LENGTH = 64  # must fit the 1-byte length prefix
# ASCII letters, digits and ". _ : -" only: e.g. "env:CONFIDENTIAL:1", "bao:vault-kek:3".
_KEK_ID = re.compile(rf"[A-Za-z0-9._:-]{{1,{KEK_ID_MAX_LENGTH}}}")
SEALED_TEXT_PREFIX = "vault1:"


# --------------------------------------------------------------------------- fields


def _uuid(value: uuid.UUID, name: str) -> bytes:
    if not isinstance(value, uuid.UUID):
        raise TypeError(f"{name} must be a uuid.UUID")
    return value.bytes


def _version(value: int) -> bytes:
    if type(value) is not int or not 1 <= value <= MAX_VERSION:  # bool is refused too
        raise ValueError(f"version must be an int in [1, {MAX_VERSION}]")
    return value.to_bytes(4, "big")


def _level(level: ConfidentialityLevel) -> bytes:
    # isinstance first: a plain str "PUBLIC" would otherwise hash like the member.
    if not isinstance(level, ConfidentialityLevel) or level not in LEVEL_TO_DOMAIN:
        raise ValueError("level must be a ConfidentialityLevel member")
    return bytes([DOMAIN_CODES[LEVEL_TO_DOMAIN[level]]])


def _is_kek_id(value: object) -> bool:
    return isinstance(value, str) and _KEK_ID.fullmatch(value) is not None


def _kek_id(kek_id: str) -> bytes:
    if not _is_kek_id(kek_id):
        raise ValueError("kek_id must be 1 to 64 characters among [A-Za-z0-9._:-]")
    raw = kek_id.encode("ascii")
    return bytes([len(raw)]) + raw


def _document_fields(
    thesis_id: uuid.UUID, document_id: uuid.UUID, version: int, level: ConfidentialityLevel
) -> bytes:
    return (
        _uuid(thesis_id, "thesis_id")
        + _uuid(document_id, "document_id")
        + _version(version)
        + _level(level)
    )


# --------------------------------------------------------------------------- associated data


def content_ad(
    *,
    thesis_id: uuid.UUID,
    document_id: uuid.UUID,
    version: int,
    level: ConfidentialityLevel,
) -> bytes:
    """Associated data of a document version's content (39 bytes)."""
    return bytes([FORMAT_V1, Context.CONTENT]) + _document_fields(
        thesis_id, document_id, version, level
    )


def dek_wrap_ad(
    *,
    thesis_id: uuid.UUID,
    document_id: uuid.UUID,
    version: int,
    level: ConfidentialityLevel,
    kek_id: str,
) -> bytes:
    """Associated data of a DEK wrapped under a KEK: binds it to its slot and its KEK."""
    return (
        bytes([FORMAT_V1, Context.DEK_WRAP])
        + _document_fields(thesis_id, document_id, version, level)
        + _kek_id(kek_id)
    )


def mfa_secret_ad(*, kek_id: str) -> bytes:
    """Associated data of an MFA secret (allauth gives encrypt() no user to bind to)."""
    return bytes([FORMAT_V1, Context.MFA_SECRET, DOMAIN_CODES[KeyDomain.MFA_SECRETS]]) + _kek_id(
        kek_id
    )


# --------------------------------------------------------------------------- containers


def pack_blob(sealed: bytes) -> bytes:
    return bytes([FORMAT_V1]) + sealed


def unpack_blob(blob: bytes) -> bytes:
    """Return the sealed part of a v1 blob; anything else is an integrity failure."""
    if not isinstance(blob, bytes) or len(blob) < 1 or blob[0] != FORMAT_V1:
        raise VaultIntegrityError(INTEGRITY_MESSAGE)
    return blob[1:]


def pack_secret(kek_id: str, sealed: bytes) -> str:
    body = base64.urlsafe_b64encode(_kek_id(kek_id) + sealed).decode("ascii")
    return SEALED_TEXT_PREFIX + body


def unpack_secret(text: str) -> tuple[str, bytes]:
    """Return (kek_id, sealed); anything malformed is an integrity failure.

    Only the canonical encoding is accepted (it must re-encode identically):
    otherwise "+" / "/" in place of "-" / "_", or non-zero padding bits, would
    give several texts for the same bytes.
    """
    if not isinstance(text, str) or not text.startswith(SEALED_TEXT_PREFIX):
        raise VaultIntegrityError(INTEGRITY_MESSAGE)
    body = text[len(SEALED_TEXT_PREFIX) :]
    try:
        raw = base64.b64decode(body, altchars=b"-_", validate=True)
    except (binascii.Error, ValueError):  # ValueError: non-ASCII input
        raise VaultIntegrityError(INTEGRITY_MESSAGE) from None
    if not raw or base64.urlsafe_b64encode(raw).decode("ascii") != body:
        raise VaultIntegrityError(INTEGRITY_MESSAGE)
    length = raw[0]
    kek_raw, sealed = raw[1 : 1 + length], raw[1 + length :]
    kek_id = kek_raw.decode("ascii", errors="replace")
    if len(kek_raw) != length or not _is_kek_id(kek_id) or not sealed:
        raise VaultIntegrityError(INTEGRITY_MESSAGE)
    return kek_id, sealed
