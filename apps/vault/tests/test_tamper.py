"""Alteration tests: blob, nonce, tag, header, associated data, level, KEK id.

Every modification must raise VaultIntegrityError with the same fixed message,
no chained cause and no byte of key or data in it.

Each helper first checks that the UNaltered value decrypts: otherwise a broken
encryption (e.g. associated data dropped at sealing) would make every
"alteration is detected" test pass for the wrong reason.
"""

import sys
import uuid
from typing import Any

import pytest
from django.views.debug import SafeExceptionReporterFilter

from apps.core.confidentiality import ConfidentialityLevel as L
from apps.vault import crypto
from apps.vault import formats as f
from apps.vault.errors import INTEGRITY_MESSAGE, VaultIntegrityError

T, D = uuid.uuid4(), uuid.uuid4()
FIELDS: dict[str, Any] = {"thesis_id": T, "document_id": D, "version": 3, "level": L.CONFIDENTIAL}
PLAINTEXT = b"Fictitious minutes of a fictitious meeting."


def _assert_integrity_failure(excinfo: pytest.ExceptionInfo[VaultIntegrityError]) -> None:
    assert str(excinfo.value) == INTEGRITY_MESSAGE
    assert excinfo.value.__cause__ is None
    assert excinfo.value.__suppress_context__ or excinfo.value.__context__ is None


def _decrypt_document(dek: bytes, blob: bytes, **fields: Any) -> bytes:
    return crypto.aead_decrypt(dek, f.unpack_blob(blob), f.content_ad(**(FIELDS | fields)))


def _encrypt_document() -> tuple[bytes, bytes]:
    dek = crypto.new_dek()
    blob = f.pack_blob(crypto.aead_encrypt(dek, PLAINTEXT, f.content_ad(**FIELDS)))
    assert _decrypt_document(dek, blob) == PLAINTEXT  # control
    return dek, blob


def test_round_trip() -> None:
    dek, blob = _encrypt_document()
    assert _decrypt_document(dek, blob) == PLAINTEXT


def test_plaintext_does_not_appear_in_the_blob() -> None:
    _, blob = _encrypt_document()
    assert PLAINTEXT not in blob
    assert PLAINTEXT[:8] not in blob


def test_blob_layout() -> None:
    _, blob = _encrypt_document()
    assert blob[0] == f.FORMAT_V1
    assert len(blob) == 1 + crypto.NONCE_BYTES + len(PLAINTEXT) + crypto.TAG_BYTES


# --------------------------------------------------------------------------- blob bytes


def test_every_single_bit_flip_of_the_blob_is_detected() -> None:
    """Covers the header (byte 0), the nonce (1..12), the ciphertext and the tag."""
    dek, blob = _encrypt_document()
    for i in range(len(blob) * 8):
        altered = bytearray(blob)
        altered[i // 8] ^= 1 << (i % 8)
        with pytest.raises(VaultIntegrityError) as excinfo:
            _decrypt_document(dek, bytes(altered))
        _assert_integrity_failure(excinfo)


@pytest.mark.parametrize(
    "alter",
    [
        lambda b: b[:-1],  # tag truncated
        lambda b: b + b"\x00",  # byte appended
        lambda b: b[:1] + b[2:],  # nonce shortened, everything shifted
        lambda b: b[: 1 + 12 + 16 - 1],  # shorter than nonce + tag
        lambda b: b[:1],  # header only
        lambda b: b"",
    ],
    ids=["truncated", "extended", "shifted", "too-short", "header-only", "empty"],
)
def test_truncated_or_extended_blob_is_detected(alter: Any) -> None:
    dek, blob = _encrypt_document()
    with pytest.raises(VaultIntegrityError) as excinfo:
        _decrypt_document(dek, alter(blob))
    _assert_integrity_failure(excinfo)


def test_nonce_swapped_with_another_blob_is_detected() -> None:
    dek, blob = _encrypt_document()
    _, other = _encrypt_document()
    spliced = blob[:1] + other[1:13] + blob[13:]
    with pytest.raises(VaultIntegrityError):
        _decrypt_document(dek, spliced)


def test_wrong_key_is_detected() -> None:
    _, blob = _encrypt_document()
    with pytest.raises(VaultIntegrityError):
        _decrypt_document(crypto.new_dek(), blob)


def test_wrong_key_and_wrong_tag_fail_identically() -> None:
    """No oracle: the caller cannot tell a wrong key from an altered tag."""
    dek, blob = _encrypt_document()
    bad_tag = blob[:-1] + bytes([blob[-1] ^ 1])
    with pytest.raises(VaultIntegrityError) as wrong_key:
        _decrypt_document(crypto.new_dek(), blob)
    with pytest.raises(VaultIntegrityError) as wrong_tag:
        _decrypt_document(dek, bad_tag)
    for excinfo in (wrong_key, wrong_tag):
        _assert_integrity_failure(excinfo)
    assert type(wrong_key.value) is type(wrong_tag.value)
    assert wrong_key.value.args == wrong_tag.value.args == (INTEGRITY_MESSAGE,)


@pytest.mark.parametrize("length", range(crypto.NONCE_BYTES + crypto.TAG_BYTES))
def test_every_ciphertext_shorter_than_nonce_and_tag_is_refused(length: int) -> None:
    """0 to 27 bytes: not even an empty plaintext fits (28 bytes)."""
    with pytest.raises(VaultIntegrityError) as excinfo:
        crypto.aead_decrypt(crypto.new_dek(), b"\x00" * length, b"ad")
    _assert_integrity_failure(excinfo)


# --------------------------------------------------------------------------- associated data


@pytest.mark.parametrize(
    "change",
    [
        {"thesis_id": uuid.uuid4()},
        {"document_id": uuid.uuid4()},
        {"version": 2},
        {"version": 4},
        {"thesis_id": D, "document_id": T},
    ],
    ids=["thesis", "document", "previous-version", "next-version", "ids-swapped"],
)
def test_changed_associated_data_is_detected(change: dict[str, Any]) -> None:
    dek, blob = _encrypt_document()
    with pytest.raises(VaultIntegrityError) as excinfo:
        _decrypt_document(dek, blob, **change)
    _assert_integrity_failure(excinfo)


@pytest.mark.parametrize("level", [lv for lv in L if lv != L.CONFIDENTIAL])
def test_changed_level_is_detected(level: L) -> None:
    """Acceptance criterion: a document cannot change level without re-encryption."""
    dek, blob = _encrypt_document()
    with pytest.raises(VaultIntegrityError):
        _decrypt_document(dek, blob, level=level)


def test_wrap_ad_cannot_stand_in_for_content_ad() -> None:
    dek, blob = _encrypt_document()
    wrap_ad = f.dek_wrap_ad(**FIELDS, kek_id="env:CONFIDENTIAL:1")
    with pytest.raises(VaultIntegrityError):
        crypto.aead_decrypt(dek, f.unpack_blob(blob), wrap_ad)
    with pytest.raises(VaultIntegrityError):
        crypto.aead_decrypt(dek, f.unpack_blob(blob), wrap_ad[:39])


# --------------------------------------------------------------------------- DEK wrapping


def _wrap(kek_id: str = "env:CONFIDENTIAL:1") -> tuple[bytes, bytes, bytes]:
    kek, dek = crypto.derive_kek("CONFIDENTIAL", 1), crypto.new_dek()
    wrapped = crypto.aead_encrypt(kek, dek, f.dek_wrap_ad(**FIELDS, kek_id=kek_id))
    assert crypto.aead_decrypt(kek, wrapped, f.dek_wrap_ad(**FIELDS, kek_id=kek_id)) == dek
    return kek, dek, wrapped


def test_wrapped_dek_layout_and_round_trip() -> None:
    kek, dek, wrapped = _wrap()
    assert len(wrapped) == 60  # nonce 12 + DEK 32 + tag 16
    assert dek not in wrapped
    assert crypto.aead_decrypt(kek, wrapped, f.dek_wrap_ad(**FIELDS, kek_id="env:CONFIDENTIAL:1"))


@pytest.mark.parametrize(
    "change",
    [
        {"kek_id": "env:CONFIDENTIAL:2"},
        {"kek_id": "env:PUBLIC:1"},
        {"document_id": uuid.uuid4()},
        {"thesis_id": uuid.uuid4()},
        {"version": 4},
        {"level": L.PUBLIC},
    ],
    ids=["kek-version", "kek-domain", "document", "thesis", "version", "level"],
)
def test_wrapped_dek_is_bound_to_its_slot_and_kek(change: dict[str, Any]) -> None:
    kek, _, wrapped = _wrap()
    ad = f.dek_wrap_ad(**({**FIELDS, "kek_id": "env:CONFIDENTIAL:1"} | change))
    with pytest.raises(VaultIntegrityError):
        crypto.aead_decrypt(kek, wrapped, ad)


def test_every_bit_flip_of_the_wrapped_dek_is_detected() -> None:
    kek, _, wrapped = _wrap()
    ad = f.dek_wrap_ad(**FIELDS, kek_id="env:CONFIDENTIAL:1")
    for i in range(len(wrapped) * 8):
        altered = bytearray(wrapped)
        altered[i // 8] ^= 1 << (i % 8)
        with pytest.raises(VaultIntegrityError):
            crypto.aead_decrypt(kek, bytes(altered), ad)


def test_other_kek_cannot_unwrap() -> None:
    _, _, wrapped = _wrap()
    ad = f.dek_wrap_ad(**FIELDS, kek_id="env:CONFIDENTIAL:1")
    with pytest.raises(VaultIntegrityError):
        crypto.aead_decrypt(crypto.derive_kek("CONFIDENTIAL", 2), wrapped, ad)


# --------------------------------------------------------------------------- MFA secret


def test_mfa_secret_kek_id_is_authenticated() -> None:
    kek = crypto.derive_kek("MFA_SECRETS", 1)
    text = f.pack_secret(
        "env:MFA_SECRETS:1",
        crypto.aead_encrypt(kek, b"JBSWY3DPEHPK3PXP", f.mfa_secret_ad(kek_id="env:MFA_SECRETS:1")),
    )
    kek_id, sealed = f.unpack_secret(text)
    assert crypto.aead_decrypt(kek, sealed, f.mfa_secret_ad(kek_id=kek_id)) == b"JBSWY3DPEHPK3PXP"
    forged = f.pack_secret("env:MFA_SECRETS:2", sealed)
    kek_id, sealed2 = f.unpack_secret(forged)
    with pytest.raises(VaultIntegrityError):
        crypto.aead_decrypt(kek, sealed2, f.mfa_secret_ad(kek_id=kek_id))


# --------------------------------------------------------------------------- nonces and keys


def test_nonces_never_repeat_and_ciphertexts_differ() -> None:
    key, ad = crypto.new_dek(), b"ad"
    sealed = [crypto.aead_encrypt(key, b"same plaintext", ad) for _ in range(5_000)]
    assert len({s[: crypto.NONCE_BYTES] for s in sealed}) == 5_000
    assert len(set(sealed)) == 5_000


def test_deks_are_fresh_32_byte_keys() -> None:
    deks = {crypto.new_dek() for _ in range(1_000)}
    assert len(deks) == 1_000
    assert {len(d) for d in deks} == {32}


@pytest.mark.parametrize("ad", [b"", None, "ad", bytearray(b"ad")])
def test_empty_or_non_bytes_associated_data_is_refused(ad: Any) -> None:
    key = crypto.new_dek()
    with pytest.raises(ValueError):
        crypto.aead_encrypt(key, b"x", ad)
    with pytest.raises(ValueError):
        crypto.aead_decrypt(key, b"\x00" * 40, ad)


@pytest.mark.parametrize("key", [b"", b"k" * 16, b"k" * 24, b"k" * 33, "k" * 32, None])
def test_only_256_bit_keys_are_accepted(key: Any) -> None:
    with pytest.raises(ValueError) as excinfo:
        crypto.aead_encrypt(key, b"x", b"ad")
    assert "k" * 16 not in str(excinfo.value)


def test_non_bytes_ciphertext_is_an_integrity_failure() -> None:
    with pytest.raises(VaultIntegrityError):
        crypto.aead_decrypt(crypto.new_dek(), "not bytes", b"ad")  # type: ignore[arg-type]


# ---------------------------------------------------------------------- no key in error reports


def test_error_reports_hide_keys_and_plaintext(settings: Any) -> None:
    """With DEBUG=False, Django's report filter stars every local of the crypto frames."""
    settings.DEBUG = False
    dek, blob = _encrypt_document()
    try:
        _decrypt_document(dek, blob, version=99)
    except VaultIntegrityError:
        tb = sys.exc_info()[2]
    report_filter = SafeExceptionReporterFilter()
    shown: list[object] = []
    while tb is not None:
        frame = tb.tb_frame
        if frame.f_code.co_filename.endswith("vault/crypto.py"):
            shown.extend(
                value for _, value in report_filter.get_traceback_frame_variables(None, frame)
            )
        tb = tb.tb_next
    assert shown  # crypto frames were inspected
    assert all(value == report_filter.cleansed_substitute for value in shown)


@pytest.mark.parametrize(
    "func",
    [
        crypto.aead_encrypt,
        crypto.aead_decrypt,
        crypto._seal,
        crypto._open,
        crypto.derive_subkey,
        crypto.derive_kek,
        crypto.ed25519_public_key,
        crypto.ed25519_sign,
    ],
)
def test_key_handling_functions_are_marked_sensitive(func: Any) -> None:
    assert func.__code__.co_name == "sensitive_variables_wrapper"
