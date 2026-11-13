"""Official test vectors for every primitive vault.crypto calls.

- AES-256-GCM: NIST CAVP (gcmEncryptExtIV256 / gcmDecrypt256) and Project
  Wycheproof, both embedded in tests/vectors/ with their source and its SHA-256;
- HKDF-SHA256: RFC 5869, test case 3 (zero-length salt = our salt=None);
- Ed25519: RFC 8032, section 7.1, tests 1 to 3;
- SHA-256: FIPS 180-4 examples ("abc", empty string).

AES-GCM vectors go through crypto._seal / crypto._open, the exact calls the
public functions make; the public functions additionally refuse empty
associated data, which some vectors use.
"""

import json
from pathlib import Path
from typing import Any

import pytest

from apps.vault import crypto
from apps.vault.errors import VaultIntegrityError

VECTORS = Path(__file__).parent / "vectors"
NIST = json.loads((VECTORS / "aes256gcm_nist_cavp.json").read_text())
WYCHEPROOF = json.loads((VECTORS / "aes256gcm_wycheproof.json").read_text())

h = bytes.fromhex


def _ids(cases: list[dict[str, Any]]) -> list[str]:
    return [f"pt{len(c['ct']) // 2}-aad{len(c['aad']) // 2}" for c in cases]


# --------------------------------------------------------------------------- AES-256-GCM (NIST)


def test_nist_selection_covers_every_length_group() -> None:
    for kind in ("encrypt", "decrypt_valid", "decrypt_fail"):
        # FAIL cases have no plaintext: group by ciphertext length (= plaintext length).
        groups = {(len(c["ct"]), len(c["aad"])) for c in NIST[kind]}
        assert len(groups) == 25, kind  # 5 plaintext lengths x 5 AAD lengths


@pytest.mark.parametrize("case", NIST["encrypt"], ids=_ids(NIST["encrypt"]))
def test_nist_gcm_encrypt(case: dict[str, str]) -> None:
    sealed = crypto._seal(h(case["key"]), h(case["iv"]), h(case["pt"]), h(case["aad"]))
    assert sealed == h(case["iv"] + case["ct"] + case["tag"])


@pytest.mark.parametrize("case", NIST["decrypt_valid"], ids=_ids(NIST["decrypt_valid"]))
def test_nist_gcm_decrypt_valid(case: dict[str, str]) -> None:
    sealed = h(case["iv"] + case["ct"] + case["tag"])
    assert crypto._open(h(case["key"]), sealed, h(case["aad"])) == h(case["pt"])


@pytest.mark.parametrize(
    "case",
    NIST["decrypt_fail"],
    ids=[f"ct{len(c['ct']) // 2}-aad{len(c['aad']) // 2}" for c in NIST["decrypt_fail"]],
)
def test_nist_gcm_decrypt_fail(case: dict[str, str]) -> None:
    sealed = h(case["iv"] + case["ct"] + case["tag"])
    with pytest.raises(VaultIntegrityError):
        crypto._open(h(case["key"]), sealed, h(case["aad"]))


def test_public_api_matches_nist_when_nonce_is_fixed(monkeypatch: pytest.MonkeyPatch) -> None:
    case = next(c for c in NIST["encrypt"] if c["aad"] and c["pt"])
    monkeypatch.setattr(crypto, "_new_nonce", lambda: h(case["iv"]))
    sealed = crypto.aead_encrypt(h(case["key"]), h(case["pt"]), h(case["aad"]))
    assert sealed == h(case["iv"] + case["ct"] + case["tag"])
    assert crypto.aead_decrypt(h(case["key"]), sealed, h(case["aad"])) == h(case["pt"])


# ------------------------------------------------------------------ AES-256-GCM (Wycheproof)


def test_wycheproof_group_is_complete() -> None:
    results = [t["result"] for t in WYCHEPROOF["tests"]]
    assert len(results) == 66
    assert results.count("invalid") == 27


@pytest.mark.parametrize("t", WYCHEPROOF["tests"], ids=lambda t: f"tc{t['tcId']}")
def test_wycheproof(t: dict[str, Any]) -> None:
    key, iv, aad = h(t["key"]), h(t["iv"]), h(t["aad"])
    sealed = iv + h(t["ct"]) + h(t["tag"])
    if t["result"] == "invalid":
        with pytest.raises(VaultIntegrityError):
            crypto._open(key, sealed, aad)
    else:
        assert crypto._seal(key, iv, h(t["msg"]), aad) == sealed
        assert crypto._open(key, sealed, aad) == h(t["msg"])


# --------------------------------------------------------------------------- HKDF (RFC 5869)

# RFC 5869, A.3: SHA-256, IKM = 22 x 0x0b, zero-length salt and info, L = 42.
# A zero-length salt is replaced by HashLen zero bytes, as salt=None is; the
# first 32 output bytes are T(1), so they equal our 32-byte derivation.
RFC5869_A3_OKM = (
    "8da4e775a563c18f715f802a063c5a31b8a11f5c5ee1879ec3454e5f3c738d2d9d201395faa4b61a96c8"
)


def test_hkdf_matches_rfc5869_test_case_3() -> None:
    assert crypto._hkdf(h("0b" * 22), b"") == h(RFC5869_A3_OKM)[:32]


def test_kek_info_layout() -> None:
    assert crypto.kek_info("CONFIDENTIAL", 1) == b"vault-kek-v1|CONFIDENTIAL|1"
    assert crypto.kek_info("MFA_SECRETS", 12) == b"vault-kek-v1|MFA_SECRETS|12"


def test_derive_kek_is_hkdf_of_master_key_with_the_documented_info() -> None:
    assert crypto.derive_kek("INTERNAL_ST", 2) == crypto.derive_subkey(
        b"vault-kek-v1|INTERNAL_ST|2"
    )


def test_keks_differ_across_domains_and_versions() -> None:
    keks = {
        crypto.derive_kek(label, version)
        for label in ("PUBLIC", "INTERNAL_LIRMM", "INTERNAL_ST", "CONFIDENTIAL", "MFA_SECRETS")
        for version in (1, 2, 11)
    }
    assert len(keks) == 15
    assert crypto.derive_subkey(crypto.PSEUDONYM_INFO) not in keks


@pytest.mark.parametrize(
    ("label", "version"),
    [
        ("confidential", 1),
        ("", 1),
        ("A|B", 1),
        ("A" * 33, 1),
        ("PUBLIC", 0),
        ("PUBLIC", -1),
        ("PUBLIC", 2**32),
        ("PUBLIC", True),
        ("PUBLIC", "1"),
    ],
)
def test_kek_info_rejects_bad_label_or_version(label: Any, version: Any) -> None:
    with pytest.raises(ValueError):
        crypto.kek_info(label, version)


# --------------------------------------------------------------------------- Ed25519 (RFC 8032)

RFC8032 = [
    (  # TEST 1
        "9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60",
        "d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a",
        "",
        "e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e06522490155"
        "5fb8821590a33bacc61e39701cf9b46bd25bf5f0595bbe24655141438e7a100b",
    ),
    (  # TEST 2
        "4ccd089b28ff96da9db6c346ec114e0f5b8a319f35aba624da8cf6ed4fb8a6fb",
        "3d4017c3e843895a92b70aa74d1b7ebc9c982ccf2ec4968cc0cd55f12af4660c",
        "72",
        "92a009a9f0d4cab8720e820b5f642540a2b27b5416503f8fb3762223ebdb69da"
        "085ac1e43e15996e458f3613d0f11d8c387b2eaeb4302aeeb00d291612bb0c00",
    ),
    (  # TEST 3
        "c5aa8df43f9f837bedb7442f31dcb7b166d38535076f094b85ce3a2e0b4458f7",
        "fc51cd8e6218a1a38da47ed00230f0580816ed13ba3303ac5deb911548908025",
        "af82",
        "6291d657deec24024827e69c3abe01a30ce548a284743a445e3680d7db5ac3ac"
        "18ff9b538d16f290ae67f760984dc6594a7c15e9716ed28dc027beceea1ec40a",
    ),
]


@pytest.mark.parametrize(("seed", "public", "message", "signature"), RFC8032)
def test_ed25519_rfc8032(seed: str, public: str, message: str, signature: str) -> None:
    assert crypto.ed25519_public_key(h(seed)) == h(public)
    assert crypto.ed25519_sign(h(seed), h(message)) == h(signature)
    assert crypto.ed25519_verify(h(public), h(signature), h(message))


@pytest.mark.parametrize(("seed", "public", "message", "signature"), RFC8032)
def test_ed25519_rejects_any_flipped_bit(
    seed: str, public: str, message: str, signature: str
) -> None:
    sig = h(signature)
    for i in range(len(sig) * 8):
        forged = bytearray(sig)
        forged[i // 8] ^= 1 << (i % 8)
        assert not crypto.ed25519_verify(h(public), bytes(forged), h(message))
    assert not crypto.ed25519_verify(h(public), sig, h(message) + b"\x00")


@pytest.mark.parametrize("length", [0, 1, 31, 33, 64])
def test_ed25519_public_key_of_wrong_length_is_false_not_an_error(length: int) -> None:
    _, public, message, signature = RFC8032[1]
    assert not crypto.ed25519_verify(
        h(public)[:length].ljust(length, b"\x00"), h(signature), h(message)
    )


@pytest.mark.parametrize("length", [0, 1, 63, 65, 128])
def test_ed25519_signature_of_wrong_length_is_false_not_an_error(length: int) -> None:
    _, public, message, signature = RFC8032[1]
    forged = (h(signature) * 2)[:length]
    assert not crypto.ed25519_verify(h(public), forged, h(message))


# --------------------------------------------------------------------------- SHA-256 (FIPS 180-4)


def test_sha256_fips_180_4_examples() -> None:
    assert crypto.sha256(b"abc") == h(
        "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
    )
    assert crypto.sha256(b"") == h(
        "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    )


def test_digests_equal_uses_compare_digest(monkeypatch: pytest.MonkeyPatch) -> None:
    import hmac

    calls: list[object] = []
    real = hmac.compare_digest

    def spy(a: bytes, b: bytes) -> bool:
        calls.append(a)
        return real(a, b)

    monkeypatch.setattr(hmac, "compare_digest", spy)
    assert crypto.digests_equal(b"x" * 32, b"x" * 32)
    assert not crypto.digests_equal(b"x" * 32, b"y" * 32)
    assert len(calls) == 2
