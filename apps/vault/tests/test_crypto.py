"""vault.crypto: tokens and keyed pseudonyms (M1 part of the module)."""

import base64
import hashlib
import hmac
import re
import secrets
from typing import Any

import pytest
from django.core.exceptions import ImproperlyConfigured

from apps.vault import crypto


def test_new_token_has_32_random_bytes() -> None:
    token = crypto.new_token()
    raw = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))
    assert len(raw) == 32
    assert re.fullmatch(r"[A-Za-z0-9_-]{43}", token)


def test_new_tokens_differ() -> None:
    assert len({crypto.new_token() for _ in range(100)}) == 100


def test_hash_token_is_sha256_hex() -> None:
    assert crypto.hash_token("abc") == hashlib.sha256(b"abc").hexdigest()


def test_tokens_match() -> None:
    token = crypto.new_token()
    stored = crypto.hash_token(token)
    assert crypto.tokens_match(token, stored)
    assert not crypto.tokens_match(crypto.new_token(), stored)


@pytest.mark.parametrize("bad", ["", "é" * 43, "x" * 10_000])
def test_tokens_match_rejects_odd_input_without_error(bad: str) -> None:
    assert not crypto.tokens_match(bad, crypto.hash_token(crypto.new_token()))


def test_tokens_match_uses_constant_time_comparison(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, str]] = []
    real = hmac.compare_digest

    def spy(a: str, b: str) -> bool:
        calls.append((a, b))
        return bool(real(a, b))

    monkeypatch.setattr(hmac, "compare_digest", spy)
    token = crypto.new_token()
    crypto.tokens_match(token, crypto.hash_token(token))
    assert len(calls) == 1


def test_pseudonym_is_keyed_and_purpose_bound() -> None:
    a = crypto.pseudonym("192.0.2.1", purpose="ip")
    assert a == crypto.pseudonym("192.0.2.1", purpose="ip")
    assert a != crypto.pseudonym("192.0.2.1", purpose="email")
    assert a != crypto.pseudonym("192.0.2.2", purpose="ip")
    assert re.fullmatch(r"[0-9a-f]{64}", a)


def test_pseudonym_is_not_a_plain_hash() -> None:
    assert crypto.pseudonym("192.0.2.1", purpose="ip") != hashlib.sha256(b"192.0.2.1").hexdigest()


@pytest.mark.parametrize("purpose", ["", "IP", "ip\x00x", "a" * 40, "é"])
def test_pseudonym_rejects_odd_purpose(purpose: str) -> None:
    with pytest.raises(ValueError):
        crypto.pseudonym("x", purpose=purpose)


# --------------------------------------------------------------------------- HKDF sub-key


def _hkdf_reference(ikm: bytes, info: bytes, length: int = 32) -> bytes:
    """RFC 5869 written out with the stdlib, as an independent check of the library call."""
    prk = hmac.new(b"\x00" * 32, ikm, hashlib.sha256).digest()  # salt=None -> zeros
    okm, block = b"", b""
    for i in range(1, -(-length // 32) + 1):
        block = hmac.new(prk, block + info + bytes([i]), hashlib.sha256).digest()
        okm += block
    return okm[:length]


def _set_master(settings: Any, key: bytes) -> None:
    settings.MASTER_KEY = base64.b64encode(key).decode()


# Published outputs of RFC 5869 test case A.1: public values, not secrets.
RFC5869_A1_PRK = (
    "077709362c2e32df0ddc3f0dc47bba6390b6c73bb50f9c3122ec844ad7c2b3e5"  # pragma: allowlist secret
)
RFC5869_A1_OKM = (
    "3cb25f25faacd57a90434f64d0362f2a2d2d0a90cf1a5a4c5d"  # pragma: allowlist secret
    "b02d56ecc4c5bf34007208d5b887185865"  # pragma: allowlist secret
)


def test_reference_hkdf_matches_rfc5869_test_case_1() -> None:
    """Check the reference itself first (RFC 5869, A.1, with its non-empty salt)."""
    ikm = bytes.fromhex("0b" * 22)
    salt = bytes.fromhex("000102030405060708090a0b0c")
    info = bytes.fromhex("f0f1f2f3f4f5f6f7f8f9")
    prk = hmac.new(salt, ikm, hashlib.sha256).digest()
    assert prk.hex() == RFC5869_A1_PRK
    okm = b""
    block = b""
    for i in (1, 2):
        block = hmac.new(prk, block + info + bytes([i]), hashlib.sha256).digest()
        okm += block
    assert okm[:42].hex() == RFC5869_A1_OKM


def test_subkey_is_hkdf_sha256_of_master_key(settings: Any) -> None:
    master = secrets.token_bytes(32)
    _set_master(settings, master)
    assert crypto.derive_subkey(b"audit-pseudonym-v1") == _hkdf_reference(
        master, b"audit-pseudonym-v1"
    )


def test_subkey_differs_from_master_key(settings: Any) -> None:
    master = secrets.token_bytes(32)
    _set_master(settings, master)
    subkey = crypto.derive_subkey(crypto.PSEUDONYM_INFO)
    assert len(subkey) == 32
    assert subkey != master


def test_subkey_changes_with_info() -> None:
    assert crypto.derive_subkey(b"audit-pseudonym-v1") != crypto.derive_subkey(
        b"audit-pseudonym-v2"
    )


def test_pseudonym_uses_the_dedicated_subkey() -> None:
    key = crypto.derive_subkey(b"audit-pseudonym-v1")
    expected = hmac.new(key, b"ip\x00192.0.2.1", hashlib.sha256).hexdigest()
    assert crypto.pseudonym("192.0.2.1", purpose="ip") == expected


def test_secret_key_rotation_does_not_change_pseudonyms(settings: Any) -> None:
    before = crypto.pseudonym("192.0.2.1", purpose="ip")
    settings.SECRET_KEY = secrets.token_urlsafe(50)
    assert crypto.pseudonym("192.0.2.1", purpose="ip") == before


def test_master_key_change_changes_pseudonyms(settings: Any) -> None:
    before = crypto.pseudonym("192.0.2.1", purpose="ip")
    _set_master(settings, secrets.token_bytes(32))
    assert crypto.pseudonym("192.0.2.1", purpose="ip") != before


@pytest.mark.parametrize(
    "value",
    ["", "not base64!", base64.b64encode(b"x" * 16).decode(), base64.b64encode(b"x" * 33).decode()],
)
def test_invalid_master_key_is_refused_without_echoing_it(settings: Any, value: str) -> None:
    settings.MASTER_KEY = value
    with pytest.raises(ImproperlyConfigured) as excinfo:
        crypto.pseudonym("192.0.2.1", purpose="ip")
    if value:
        assert value not in str(excinfo.value)


@pytest.mark.parametrize("info", [b"", "audit-pseudonym-v1"])
def test_derive_subkey_requires_a_bytes_label(info: Any) -> None:
    with pytest.raises(ValueError):
        crypto.derive_subkey(info)
