"""Byte layouts of vault format v1: associated data, blob, MFA secret, sealed text.

Pure encoding: no cryptography here (CLAUDE.md rule 1, see crypto.py).

Every layout is injective: fixed-length fields first, then at most one
variable field, prefixed by its length, at the very end. The first byte is the
format version and the second a context byte, so that encodings made for
different purposes never coincide. Changing a layout means a new format byte;
data written under v1 must stay decodable.

    content AD   = 0x01 | 0x01 | thesis_id(16) | document_id(16) | version(4, BE) | level(1)
    DEK-wrap AD  = 0x01 | 0x02 | thesis_id | document_id | version | level | len(1) | kek_id
    MFA AD       = 0x01 | 0x03 | 0x80 | len(1) | kek_id
    text AD      = 0x01 | 0x04 | thesis_id(16) | object_id(16) | slot(2, BE) | level(1)
    text-wrap AD = 0x01 | 0x05 | thesis_id | object_id | slot | level | len(1) | kek_id
    blob         = 0x01 | sealed            (sealed = nonce | ciphertext | tag, see crypto)
    sealed MFA   = "vault1:" + base64url(len(1) | kek_id | sealed)
    sealed text  = blob of pad_text(UTF-8 text), stored in vault.SealedText

A sealed text is bound to (thesis, object, slot, level), the slot naming the
model and the field ("objectives.Objective.title") by a FROZEN 2-byte code
(TEXT_SLOTS): one fixed-length field instead of two variable ones.
Its plaintext is padded (ISO/IEC 7816-4) to a multiple of PAD_BLOCK bytes
before encryption, so the stored length reveals only a 64-byte bucket.

canonical_json() is the RFC 8785 subset hashed by the audit chain.

kek_id is deliberately absent from the content AD: rotating a KEK re-wraps the
DEKs only and leaves the blobs untouched (docs/SECURITY.md).
"""

from __future__ import annotations

import base64
import binascii
import json
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
    SEALED_TEXT = 0x04
    SEALED_TEXT_DEK_WRAP = 0x05


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
SEALED_TEXT_PREFIX = "vault1:"  # MFA secrets (a text column of allauth)

# --------------------------------------------------------------------------- text slots

# FROZEN, like DOMAIN_CODES: a slot code is inside the associated data of every
# text sealed under it. Never change a code, never give a code to another name.
# A slot that is no longer used (no row left) moves to RETIRED_SLOT_CODES, and
# its code is never assigned again (tests/test_text_formats.py lists every code
# ever assigned).
#
# Ranges reserved per module (high byte); 0xXX00 is reserved in every range:
#   0x00xx  never used
#   0x01xx  objectives (M4)        0x04xx  travel (M7)
#   0x02xx  meetings (M5)          0x05xx  reports (M8)
#   0x03xx  conferences (M6)       0x06xx  portfolio (M9)
#   0x07xx-0xFFxx  free, to be reserved here before use
SLOT_RANGES: Mapping[int, str] = MappingProxyType(
    {
        0x01: "objectives",
        0x02: "meetings",
        0x03: "conferences",
        0x04: "travel",
        0x05: "reports",
        0x06: "portfolio",
    }
)

# "<app_label>.<Model>.<field>" -> code (the model's _meta.label, then the field).
TEXT_SLOTS: Mapping[str, int] = MappingProxyType(
    {
        "objectives.Objective.title": 0x0101,
        "objectives.Objective.description": 0x0102,
        "objectives.Objective.success_criterion": 0x0103,
        "objectives.ObjectiveRevision.reason": 0x0104,
        "objectives.ObjectiveRevision.old_value": 0x0105,
        "objectives.ObjectiveRevision.new_value": 0x0106,
        "objectives.ProgressUpdate.note": 0x0107,
        "objectives.ObjectiveComment.body": 0x0108,
        "meetings.Meeting.title": 0x0201,
        "meetings.Meeting.agenda": 0x0202,
        "meetings.Meeting.minutes": 0x0203,
        "meetings.Meeting.location_detail": 0x0204,
        "meetings.Meeting.visio_link": 0x0205,
        "meetings.Decision.text": 0x0206,
        "meetings.ActionItem.title": 0x0207,
        "conferences.Submission.title": 0x0301,
        "conferences.Submission.abstract": 0x0302,
        "conferences.Submission.external_authors": 0x0303,
        "conferences.Submission.notes": 0x0304,
        "conferences.StApproval.comment": 0x0305,
        "conferences.Preprint.url": 0x0306,
        "travel.Mission.destination_detail": 0x0401,
        "travel.Mission.purpose": 0x0402,
        "travel.Mission.lodging_note": 0x0403,
    }
)
RETIRED_SLOT_CODES: frozenset[int] = frozenset()
SLOT_NAMES: Mapping[int, str] = MappingProxyType({code: name for name, code in TEXT_SLOTS.items()})

PAD_BLOCK = 64
MAX_PADDED_TEXT_BYTES = 16_384
MAX_TEXT_BYTES = MAX_PADDED_TEXT_BYTES - 1  # the 0x80 marker always takes one byte


def slot_code(name: str) -> int:
    """Code of the slot ``name`` ("objectives.Objective.title"); unknown -> ValueError."""
    code = TEXT_SLOTS.get(name) if isinstance(name, str) else None
    if code is None:
        raise ValueError(f"no text slot {name!r}")
    return code


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


def _slot(code: int) -> bytes:
    if type(code) is not int:  # bool and the slot's name are refused
        raise TypeError("slot must be an int code (formats.slot_code)")
    if code not in SLOT_NAMES:
        raise ValueError(f"unknown or retired slot code {code:#06x}")
    return code.to_bytes(2, "big")


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


def _text_fields(
    thesis_id: uuid.UUID, object_id: uuid.UUID, slot: int, level: ConfidentialityLevel
) -> bytes:
    return (
        _uuid(thesis_id, "thesis_id") + _uuid(object_id, "object_id") + _slot(slot) + _level(level)
    )


def text_ad(
    *, thesis_id: uuid.UUID, object_id: uuid.UUID, slot: int, level: ConfidentialityLevel
) -> bytes:
    """Associated data of a sealed text (37 bytes)."""
    return bytes([FORMAT_V1, Context.SEALED_TEXT]) + _text_fields(thesis_id, object_id, slot, level)


def text_dek_wrap_ad(
    *,
    thesis_id: uuid.UUID,
    object_id: uuid.UUID,
    slot: int,
    level: ConfidentialityLevel,
    kek_id: str,
) -> bytes:
    """Associated data of a sealed text's DEK wrapped under a KEK."""
    return (
        bytes([FORMAT_V1, Context.SEALED_TEXT_DEK_WRAP])
        + _text_fields(thesis_id, object_id, slot, level)
        + _kek_id(kek_id)
    )


# --------------------------------------------------------------------------- padding


def pad_text(data: bytes) -> bytes:
    """ISO/IEC 7816-4: 0x80, then zeros up to the next multiple of PAD_BLOCK.

    Always adds 1 to PAD_BLOCK bytes, so the padding is never ambiguous.
    """
    if not isinstance(data, bytes):
        raise TypeError("data must be bytes")
    if len(data) > MAX_TEXT_BYTES:
        raise ValueError(f"text too long: at most {MAX_TEXT_BYTES} bytes in UTF-8")
    zeros = -(len(data) + 1) % PAD_BLOCK
    return data + b"\x80" + bytes(zeros)


def unpad_text(padded: bytes) -> bytes:
    """Inverse of pad_text; anything pad_text cannot produce is an integrity failure.

    Called on AUTHENTICATED plaintext only (after the AEAD tag was checked):
    whatever it answers, it is no padding oracle.
    """
    if (
        not isinstance(padded, bytes)
        or not padded
        or len(padded) % PAD_BLOCK
        or len(padded) > MAX_PADDED_TEXT_BYTES
    ):
        raise VaultIntegrityError(INTEGRITY_MESSAGE)
    end = len(padded.rstrip(b"\x00")) - 1  # index of the last non-zero byte
    if end < 0 or padded[end] != 0x80 or len(padded) - end > PAD_BLOCK:
        raise VaultIntegrityError(INTEGRITY_MESSAGE)
    return padded[:end]


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


# --------------------------------------------------------------------------- canonical JSON

MAX_SAFE_INTEGER = 2**53 - 1


def _utf16_order(key: str) -> bytes:
    return key.encode("utf-16-be")


def _encode_json(value: object, out: list[str]) -> None:
    if value is None:
        out.append("null")
    elif value is True:
        out.append("true")
    elif value is False:
        out.append("false")
    elif isinstance(value, int):
        if abs(value) > MAX_SAFE_INTEGER:
            raise ValueError("integer outside +-(2**53 - 1)")
        out.append(int.__repr__(value))  # never an IntEnum's name
    elif isinstance(value, str):
        try:
            value.encode("utf-8")
        except UnicodeEncodeError:
            raise ValueError("lone surrogate in a string") from None
        # json escapes exactly as RFC 8785 asks: " \ and C0 controls (\b \t \n \f \r
        # short, else \u00xx lower case); non-ASCII, "/" and DEL are left as they are.
        out.append(json.dumps(value, ensure_ascii=False))
    elif isinstance(value, list):
        out.append("[")
        for i, item in enumerate(value):
            if i:
                out.append(",")
            _encode_json(item, out)
        out.append("]")
    elif isinstance(value, dict):
        if not all(isinstance(key, str) for key in value):
            raise ValueError("object keys must be strings")
        out.append("{")
        for i, key in enumerate(sorted(value, key=_utf16_order)):
            if i:
                out.append(",")
            _encode_json(key, out)
            out.append(":")
            _encode_json(value[key], out)
        out.append("}")
    else:  # float, tuple, bytes...: outside the subset
        raise ValueError(f"{type(value).__name__} is outside the canonical JSON subset")


def canonical_json(value: object) -> bytes:
    """RFC 8785 (JCS) for the subset the audit chain uses, as UTF-8 bytes.

    Subset: null, booleans, integers within +-(2**53 - 1) (no floats), strings
    without lone surrogates, lists, objects with string keys. Keys are sorted
    by UTF-16 code units, as RFC 8785 requires (not by code points); no
    whitespace. Anything else raises ValueError.
    """
    out: list[str] = []
    _encode_json(value, out)
    return "".join(out).encode("utf-8")
