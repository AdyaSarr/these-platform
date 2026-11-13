"""vault.formats: exact v1 layouts, injectivity, strict validation."""

import base64
import random
import string
import uuid
from typing import Any

import pytest

from apps.core.confidentiality import ConfidentialityLevel as L
from apps.vault import formats as f
from apps.vault.errors import INTEGRITY_MESSAGE, VaultIntegrityError

# Written out here, NOT taken from formats.py: expected values must be independent.
EXPECTED_LEVEL_CODES = {
    L.PUBLIC: 0x01,
    L.INTERNAL_LIRMM: 0x02,
    L.INTERNAL_ST: 0x03,
    L.CONFIDENTIAL: 0x04,
}
EXPECTED_MFA_CODE = 0x80
MAX_U32 = 2**32 - 1

T = uuid.UUID("00112233-4455-6677-8899-aabbccddeeff")
D = uuid.UUID("ffeeddcc-bbaa-9988-7766-554433221100")


# --------------------------------------------------------------------------- frozen codes


def test_domain_codes_are_frozen() -> None:
    """Changing a code makes every stored ciphertext undecryptable: never silently."""
    assert dict(f.DOMAIN_CODES) == {
        "PUBLIC": 0x01,
        "INTERNAL_LIRMM": 0x02,
        "INTERNAL_ST": 0x03,
        "CONFIDENTIAL": 0x04,
        "MFA_SECRETS": 0x80,
    }
    assert f.FORMAT_V1 == 0x01
    # Append only: 0x04 and 0x05 added by sealed texts (M4).
    assert [c.value for c in f.Context] == [0x01, 0x02, 0x03, 0x04, 0x05]


def test_codes_are_unique_and_every_domain_has_one() -> None:
    assert len(set(f.DOMAIN_CODES.values())) == len(f.DOMAIN_CODES)
    assert set(f.KeyDomain) == set(f.DOMAIN_CODES)


def test_every_level_has_an_explicit_domain() -> None:
    assert set(f.LEVEL_TO_DOMAIN) == set(L)
    assert dict(f.LEVEL_TO_DOMAIN) == {
        L.PUBLIC: f.KeyDomain.PUBLIC,
        L.INTERNAL_LIRMM: f.KeyDomain.INTERNAL_LIRMM,
        L.INTERNAL_ST: f.KeyDomain.INTERNAL_ST,
        L.CONFIDENTIAL: f.KeyDomain.CONFIDENTIAL,
    }
    assert f.KeyDomain.MFA_SECRETS not in f.LEVEL_TO_DOMAIN.values()
    with pytest.raises(TypeError):
        f.LEVEL_TO_DOMAIN[L.PUBLIC] = f.KeyDomain.CONFIDENTIAL  # type: ignore[index]


def test_level_byte_matches_the_hard_coded_table() -> None:
    for level, code in EXPECTED_LEVEL_CODES.items():
        assert f.content_ad(thesis_id=T, document_id=D, version=1, level=level)[38] == code


def test_domain_mapping_is_read_only() -> None:
    with pytest.raises(TypeError):
        f.DOMAIN_CODES[f.KeyDomain.PUBLIC] = 0x09  # type: ignore[index]


# --------------------------------------------------------------------------- exact bytes


def test_content_ad_exact_bytes() -> None:
    ad = f.content_ad(thesis_id=T, document_id=D, version=0x01020304, level=L.CONFIDENTIAL)
    assert ad == bytes.fromhex(
        "01"  # format v1
        "01"  # context: content
        "00112233445566778899aabbccddeeff"  # thesis_id  # pragma: allowlist secret
        "ffeeddccbbaa99887766554433221100"  # document_id  # pragma: allowlist secret
        "01020304"  # version, big-endian
        "04"  # CONFIDENTIAL
    )
    assert len(ad) == 39


def test_dek_wrap_ad_exact_bytes() -> None:
    ad = f.dek_wrap_ad(thesis_id=T, document_id=D, version=1, level=L.PUBLIC, kek_id="env:PUBLIC:1")
    assert (
        ad
        == bytes.fromhex(
            "01"
            "02"  # context: DEK wrap
            "00112233445566778899aabbccddeeff"  # pragma: allowlist secret
            "ffeeddccbbaa99887766554433221100"  # pragma: allowlist secret
            "00000001"
            "01"  # PUBLIC
            "0c"  # len("env:PUBLIC:1") = 12
        )
        + b"env:PUBLIC:1"
    )


def test_mfa_ad_exact_bytes() -> None:
    assert f.mfa_secret_ad(kek_id="env:MFA_SECRETS:1") == (
        bytes.fromhex("01038011") + b"env:MFA_SECRETS:1"
    )


def test_kek_id_is_absent_from_content_ad() -> None:
    """Rotation re-wraps DEKs only: the content AD must not depend on the KEK."""
    ad = f.content_ad(thesis_id=T, document_id=D, version=1, level=L.INTERNAL_ST)
    assert b"env" not in ad
    assert len(ad) == 39


# --------------------------------------------------------------------------- injectivity


# Independent decoders, written from the documented layout. They use nothing from
# formats.py: levels are compared with EXPECTED_LEVEL_CODES above.


def _reference_decode_content_ad(ad: bytes) -> tuple[uuid.UUID, uuid.UUID, int, int]:
    assert len(ad) == 39
    assert ad[0] == 0x01  # format v1
    assert ad[1] == 0x01  # context: content
    thesis = uuid.UUID(bytes=ad[2:18])
    document = uuid.UUID(bytes=ad[18:34])
    version = int.from_bytes(ad[34:38], "big")
    return thesis, document, version, ad[38]


def _reference_decode_wrap_ad(ad: bytes) -> tuple[uuid.UUID, uuid.UUID, int, int, str]:
    assert ad[0] == 0x01  # format v1
    assert ad[1] == 0x02  # context: DEK wrap
    thesis = uuid.UUID(bytes=ad[2:18])
    document = uuid.UUID(bytes=ad[18:34])
    version = int.from_bytes(ad[34:38], "big")
    level = ad[38]
    n = ad[39]
    kek = ad[40:]
    assert len(kek) == n
    return thesis, document, version, level, kek.decode("ascii")


def _reference_decode_mfa_ad(ad: bytes) -> str:
    assert ad[0] == 0x01  # format v1
    assert ad[1] == 0x03  # context: MFA secret
    assert ad[2] == EXPECTED_MFA_CODE
    n = ad[3]
    kek = ad[4:]
    assert len(kek) == n
    return kek.decode("ascii")


KEK_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._:-"


def _random_fields(rng: random.Random) -> tuple[uuid.UUID, uuid.UUID, int, L, str]:
    return (
        uuid.UUID(int=rng.getrandbits(128)),
        uuid.UUID(int=rng.getrandbits(128)),
        rng.randint(1, MAX_U32),
        rng.choice(list(L)),
        "".join(rng.choice(KEK_ALPHABET) for _ in range(rng.randint(1, 64))),
    )


def test_content_ad_round_trips_through_an_independent_decoder() -> None:
    rng = random.Random(20261113)  # noqa: S311 - test data only, not key material
    seen: dict[bytes, tuple[Any, ...]] = {}
    for _ in range(10_000):
        thesis, document, version, level, _ = _random_fields(rng)
        ad = f.content_ad(thesis_id=thesis, document_id=document, version=version, level=level)
        expected = (thesis, document, version, EXPECTED_LEVEL_CODES[level])
        assert _reference_decode_content_ad(ad) == expected
        assert seen.setdefault(ad, expected) == expected  # no two inputs share an encoding


def test_wrap_ad_round_trips_through_an_independent_decoder() -> None:
    rng = random.Random(20261114)  # noqa: S311 - test data only, not key material
    seen: dict[bytes, tuple[Any, ...]] = {}
    for _ in range(10_000):
        thesis, document, version, level, kek_id = _random_fields(rng)
        ad = f.dek_wrap_ad(
            thesis_id=thesis, document_id=document, version=version, level=level, kek_id=kek_id
        )
        expected = (thesis, document, version, EXPECTED_LEVEL_CODES[level], kek_id)
        assert _reference_decode_wrap_ad(ad) == expected
        assert seen.setdefault(ad, expected) == expected


def test_mfa_ad_round_trips_through_an_independent_decoder() -> None:
    rng = random.Random(20261115)  # noqa: S311 - test data only, not key material
    seen: dict[bytes, str] = {}
    for _ in range(5_000):
        kek_id = _random_fields(rng)[4]
        ad = f.mfa_secret_ad(kek_id=kek_id)
        assert _reference_decode_mfa_ad(ad) == kek_id
        assert seen.setdefault(ad, kek_id) == kek_id


def test_swapping_thesis_and_document_changes_the_ad() -> None:
    a = f.content_ad(thesis_id=T, document_id=D, version=1, level=L.PUBLIC)
    b = f.content_ad(thesis_id=D, document_id=T, version=1, level=L.PUBLIC)
    assert a != b


def test_each_field_changes_the_content_ad() -> None:
    base: dict[str, Any] = {"thesis_id": T, "document_id": D, "version": 7, "level": L.PUBLIC}
    ref = f.content_ad(**base)
    for change in (
        {"thesis_id": uuid.uuid4()},
        {"document_id": uuid.uuid4()},
        {"version": 8},
        {"level": L.INTERNAL_LIRMM},
    ):
        assert f.content_ad(**(base | change)) != ref


def test_contexts_never_collide() -> None:
    """A content AD is never a prefix-equal stand-in for a wrap AD: byte 1 differs."""
    fields: dict[str, Any] = {"thesis_id": T, "document_id": D, "version": 1, "level": L.PUBLIC}
    content = f.content_ad(**fields)
    wrap = f.dek_wrap_ad(**fields, kek_id="k")
    mfa = f.mfa_secret_ad(kek_id="k")
    assert {content[1], wrap[1], mfa[1]} == {0x01, 0x02, 0x03}
    assert wrap[:1] + wrap[2:39] == content[:1] + content[2:]  # same fields, different context


def test_kek_id_length_prefix_prevents_extension_ambiguity() -> None:
    fields: dict[str, Any] = {"thesis_id": T, "document_id": D, "version": 1, "level": L.PUBLIC}
    a = f.dek_wrap_ad(**fields, kek_id="ab")
    b = f.dek_wrap_ad(**fields, kek_id="abc")
    assert a != b
    assert not b.startswith(a)


# --------------------------------------------------------------------------- strict inputs


@pytest.mark.parametrize("version", [0, -1, 2**32, True, 1.0, "1"])
def test_version_out_of_range_is_refused(version: Any) -> None:
    with pytest.raises(ValueError):
        f.content_ad(thesis_id=T, document_id=D, version=version, level=L.PUBLIC)


def test_maximal_version_is_accepted() -> None:
    ad = f.content_ad(thesis_id=T, document_id=D, version=2**32 - 1, level=L.PUBLIC)
    assert ad[34:38] == b"\xff\xff\xff\xff"


@pytest.mark.parametrize("value", [str(T), T.bytes, T.int, None])
def test_ids_must_be_uuid_objects(value: Any) -> None:
    with pytest.raises(TypeError):
        f.content_ad(thesis_id=value, document_id=D, version=1, level=L.PUBLIC)
    with pytest.raises(TypeError):
        f.content_ad(thesis_id=T, document_id=value, version=1, level=L.PUBLIC)


@pytest.mark.parametrize("level", ["PUBLIC", "MFA_SECRETS", f.KeyDomain.MFA_SECRETS, None, 1])
def test_level_must_be_a_confidentiality_level(level: Any) -> None:
    with pytest.raises(ValueError):
        f.content_ad(thesis_id=T, document_id=D, version=1, level=level)


@pytest.mark.parametrize("kek_id", ["", "a" * 65, "é", "a|b", "a b", "a\x00", None, b"env"])
def test_bad_kek_id_is_refused(kek_id: Any) -> None:
    with pytest.raises(ValueError):
        f.dek_wrap_ad(thesis_id=T, document_id=D, version=1, level=L.PUBLIC, kek_id=kek_id)
    with pytest.raises(ValueError):
        f.mfa_secret_ad(kek_id=kek_id)


def test_kek_id_of_64_characters_is_accepted() -> None:
    ad = f.mfa_secret_ad(kek_id="a" * 64)
    assert ad[3] == 64


def test_kek_id_limit_follows_the_constant() -> None:
    assert f.KEK_ID_MAX_LENGTH == 64  # frozen: kek_ids of this length are stored
    f.mfa_secret_ad(kek_id="a" * f.KEK_ID_MAX_LENGTH)
    with pytest.raises(ValueError):
        f.mfa_secret_ad(kek_id="a" * (f.KEK_ID_MAX_LENGTH + 1))


# --------------------------------------------------------------------------- blob


def test_blob_has_a_version_byte() -> None:
    assert f.pack_blob(b"sealed") == b"\x01sealed"
    assert f.unpack_blob(b"\x01sealed") == b"sealed"


@pytest.mark.parametrize("blob", [b"", b"\x00sealed", b"\x02sealed", b"\xffsealed", "\x01s"])
def test_unknown_or_missing_blob_header_is_an_integrity_failure(blob: Any) -> None:
    with pytest.raises(VaultIntegrityError) as excinfo:
        f.unpack_blob(blob)
    assert str(excinfo.value) == INTEGRITY_MESSAGE


# --------------------------------------------------------------------------- MFA secret


def test_secret_round_trip() -> None:
    sealed = bytes(range(40))
    text = f.pack_secret("env:MFA_SECRETS:3", sealed)
    assert text.startswith("vault1:")
    assert f.unpack_secret(text) == ("env:MFA_SECRETS:3", sealed)


def _b64(raw: bytes) -> str:
    return "vault1:" + base64.urlsafe_b64encode(raw).decode()


@pytest.mark.parametrize(
    "text",
    [
        "",
        "JBSWY3DPEHPK3PXP",  # a clear TOTP secret: never accepted as a ciphertext
        "vault2:" + base64.urlsafe_b64encode(b"\x01k" + b"x" * 30).decode(),
        "vault1:",
        "vault1:***",
        "vault1:é",
        _b64(b"\x05abc"),  # length prefix longer than the rest
        _b64(b"\x03abc"),  # nothing after the kek_id
        _b64(b"\x00" + b"x" * 30),  # empty kek_id
        _b64(b"\x03a|c" + b"x" * 30),  # kek_id outside the alphabet
        _b64(b"\x03a\xffc" + b"x" * 30),  # non-ASCII kek_id
    ],
)
def test_malformed_secret_is_an_integrity_failure(text: str) -> None:
    with pytest.raises(VaultIntegrityError) as excinfo:
        f.unpack_secret(text)
    assert str(excinfo.value) == INTEGRITY_MESSAGE
    assert excinfo.value.__cause__ is None


# --------------------------------------------------------------------------- canonical base64

# 0xfb 0xef 0xbe base64-encodes to "----" (url-safe) / "++++" (standard), and
# 0xff 0xff 0xff to "____" / "////". The packed bytes are len(1) | "k" | sealed:
# one leading filler byte puts the 3-byte groups on base64 boundaries.
TRICKY = b"\x00" + bytes.fromhex("fbefbe") * 4 + bytes.fromhex("ffffff") * 4


def test_canonical_secret_contains_url_safe_characters() -> None:
    text = f.pack_secret("k", TRICKY)  # 1 + 1 + 25 = 27 bytes: no padding
    assert "-" in text
    assert "_" in text
    assert f.unpack_secret(text) == ("k", TRICKY)


def _lenient_decode(text: str) -> bytes:
    return base64.b64decode(text[len("vault1:") :], altchars=b"-_", validate=True)


@pytest.mark.parametrize(("safe", "standard"), [("-", "+"), ("_", "/")])
def test_standard_alphabet_is_refused(safe: str, standard: str) -> None:
    text = f.pack_secret("k", TRICKY)
    forged = text.replace(safe, standard)
    # The decoding call formats.py makes accepts it and yields the same bytes:
    # only the re-encoding check refuses it.
    assert _lenient_decode(forged) == _lenient_decode(text)
    with pytest.raises(VaultIntegrityError):
        f.unpack_secret(forged)


@pytest.mark.parametrize("sealed_length", [29, 30])  # 2 + 29 = 31 bytes -> "==", 32 -> "="
def test_non_zero_padding_bits_are_refused(sealed_length: int) -> None:
    text = f.pack_secret("k", bytes(range(sealed_length)))
    assert f.unpack_secret(text)[1] == bytes(range(sealed_length))  # control
    body = text[len("vault1:") :].rstrip("=")
    padding = text[len("vault1:") + len(body) :]
    alphabet = string.ascii_uppercase + string.ascii_lowercase + string.digits + "-_"
    last = alphabet.index(body[-1])
    assert last & 1 == 0  # canonical: unused low bits are zero
    forged = "vault1:" + body[:-1] + alphabet[last | 1] + padding
    assert _lenient_decode(forged) == _lenient_decode(text)  # same bytes
    with pytest.raises(VaultIntegrityError):
        f.unpack_secret(forged)
