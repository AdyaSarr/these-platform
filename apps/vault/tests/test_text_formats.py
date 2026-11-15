"""vault.formats, sealed texts: frozen slot codes, exact AD bytes, ISO/IEC 7816-4 padding."""

import random
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

# EVERY code ever assigned, with the name it was assigned to. Append only: a slot
# that is removed from TEXT_SLOTS moves to RETIRED_SLOT_CODES and stays listed
# here, so that its code can never be given to another name.
EVER_ASSIGNED_SLOTS = {
    0x0101: "objectives.Objective.title",
    0x0102: "objectives.Objective.description",
    0x0103: "objectives.Objective.success_criterion",
    0x0104: "objectives.ObjectiveRevision.reason",
    0x0105: "objectives.ObjectiveRevision.old_value",
    0x0106: "objectives.ObjectiveRevision.new_value",
    0x0107: "objectives.ProgressUpdate.note",
    0x0108: "objectives.ObjectiveComment.body",
    0x0201: "meetings.Meeting.title",
    0x0202: "meetings.Meeting.agenda",
    0x0203: "meetings.Meeting.minutes",
    0x0204: "meetings.Meeting.location_detail",
    0x0205: "meetings.Meeting.visio_link",
    0x0206: "meetings.Decision.text",
    0x0207: "meetings.ActionItem.title",
    0x0301: "conferences.Submission.title",
    0x0302: "conferences.Submission.abstract",
    0x0303: "conferences.Submission.external_authors",
    0x0304: "conferences.Submission.notes",
    0x0305: "conferences.StApproval.comment",
    0x0306: "conferences.Preprint.url",
    0x0401: "travel.Mission.destination_detail",
    0x0402: "travel.Mission.purpose",
    0x0403: "travel.Mission.lodging_note",
    0x0404: "travel.MissionOrder.comment",
    0x0405: "travel.MissionOrder.estimated_total",
}
EXPECTED_SLOT_RANGES = {
    0x01: "objectives",
    0x02: "meetings",
    0x03: "conferences",
    0x04: "travel",
    0x05: "reports",
    0x06: "portfolio",
}

T = uuid.UUID("00112233-4455-6677-8899-aabbccddeeff")
OBJ = uuid.UUID("ffeeddcc-bbaa-9988-7766-554433221100")


# --------------------------------------------------------------------------- frozen codes


def test_text_contexts_are_frozen() -> None:
    assert f.Context.SEALED_TEXT.value == 0x04
    assert f.Context.SEALED_TEXT_DEK_WRAP.value == 0x05


def test_slot_codes_are_frozen() -> None:
    """Changing a code makes every text sealed under it undecryptable."""
    active = dict(f.TEXT_SLOTS)
    assert {code: name for name, code in active.items()} == {
        code: name for code, name in EVER_ASSIGNED_SLOTS.items() if code not in f.RETIRED_SLOT_CODES
    }
    with pytest.raises(TypeError):
        f.TEXT_SLOTS["objectives.Objective.title"] = 0x0199  # type: ignore[index]


def test_a_code_is_never_reused() -> None:
    codes = list(f.TEXT_SLOTS.values())
    assert len(codes) == len(set(codes))
    # A retired code is no longer active, and was assigned once (it is listed above).
    assert not set(codes) & f.RETIRED_SLOT_CODES
    assert EVER_ASSIGNED_SLOTS.keys() >= f.RETIRED_SLOT_CODES
    assert isinstance(f.RETIRED_SLOT_CODES, frozenset)
    # A new slot must be added to EVER_ASSIGNED_SLOTS with its name: a code
    # given to a second name fails test_slot_codes_are_frozen.
    assert set(codes) <= EVER_ASSIGNED_SLOTS.keys()


def test_slot_ranges_are_frozen_and_every_slot_is_in_its_module_range() -> None:
    assert dict(f.SLOT_RANGES) == EXPECTED_SLOT_RANGES
    for name, code in f.TEXT_SLOTS.items():
        app_label, model, field = name.split(".")
        assert model[:1].isupper() and field.isidentifier(), name
        assert 0x0101 <= code <= 0xFFFF, name
        assert f.SLOT_RANGES[code >> 8] == app_label, name
        assert code & 0xFF != 0, name  # 0xXX00 is reserved in every range


# --------------------------------------------------------------------------- exact bytes


def test_text_ad_exact_bytes() -> None:
    ad = f.text_ad(thesis_id=T, object_id=OBJ, slot=0x0101, level=L.CONFIDENTIAL)
    assert ad == bytes.fromhex(
        "01"  # format v1
        "04"  # context: sealed text
        "00112233445566778899aabbccddeeff"  # thesis_id  # pragma: allowlist secret
        "ffeeddccbbaa99887766554433221100"  # object_id  # pragma: allowlist secret
        "0101"  # slot objectives.Objective.title, big-endian
        "04"  # CONFIDENTIAL
    )
    assert len(ad) == 37


def test_text_dek_wrap_ad_exact_bytes() -> None:
    ad = f.text_dek_wrap_ad(
        thesis_id=T, object_id=OBJ, slot=0x0108, level=L.PUBLIC, kek_id="env:PUBLIC:1"
    )
    assert (
        ad
        == bytes.fromhex(
            "01"
            "05"  # context: DEK wrap of a sealed text
            "00112233445566778899aabbccddeeff"  # pragma: allowlist secret
            "ffeeddccbbaa99887766554433221100"  # pragma: allowlist secret
            "0108"  # slot objectives.ObjectiveComment.body
            "01"  # PUBLIC
            "0c"  # len("env:PUBLIC:1") = 12
        )
        + b"env:PUBLIC:1"
    )


def test_kek_id_is_absent_from_the_text_ad() -> None:
    """Rotation re-wraps DEKs only: the content AD must not depend on the KEK."""
    assert b"env" not in f.text_ad(thesis_id=T, object_id=OBJ, slot=0x0101, level=L.PUBLIC)


def test_text_ads_never_collide_with_document_ads() -> None:
    """The context byte separates them even where the lengths could match."""
    text = f.text_ad(thesis_id=T, object_id=OBJ, slot=0x0101, level=L.PUBLIC)
    document = f.content_ad(thesis_id=T, document_id=OBJ, version=1, level=L.PUBLIC)
    assert text[:2] == b"\x01\x04" and document[:2] == b"\x01\x01"


@pytest.mark.parametrize(
    "kwargs",
    [
        {"slot": 0x0100},  # reserved
        {"slot": 0x01FF},  # in range, never assigned
        {"slot": 0x10000},
        {"slot": -1},
        {"slot": True},
        {"slot": "objectives.Objective.title"},  # the code, not the name
        {"level": "PUBLIC"},  # a plain str, not the member
        {"thesis_id": str(T)},
        {"object_id": OBJ.bytes},
    ],
)
def test_text_ad_rejects_malformed_fields(kwargs: dict[str, Any]) -> None:
    fields: dict[str, Any] = {"thesis_id": T, "object_id": OBJ, "slot": 0x0101, "level": L.PUBLIC}
    fields.update(kwargs)
    with pytest.raises((TypeError, ValueError)):
        f.text_ad(**fields)


def test_slot_code_lookup() -> None:
    assert f.slot_code("objectives.Objective.title") == 0x0101
    with pytest.raises(ValueError, match="no text slot"):
        f.slot_code("objectives.Objective.weight")


# --------------------------------------------------------------------------- injectivity


def _reference_decode_text_ad(ad: bytes) -> tuple[uuid.UUID, uuid.UUID, int, int]:
    assert len(ad) == 37
    assert ad[0] == 0x01  # format v1
    assert ad[1] == 0x04  # context: sealed text
    return (
        uuid.UUID(bytes=ad[2:18]),
        uuid.UUID(bytes=ad[18:34]),
        int.from_bytes(ad[34:36], "big"),
        ad[36],
    )


def _reference_decode_text_wrap_ad(ad: bytes) -> tuple[uuid.UUID, uuid.UUID, int, int, str]:
    assert ad[0] == 0x01
    assert ad[1] == 0x05  # context: DEK wrap of a sealed text
    n = ad[37]
    kek = ad[38:]
    assert len(kek) == n
    return (
        uuid.UUID(bytes=ad[2:18]),
        uuid.UUID(bytes=ad[18:34]),
        int.from_bytes(ad[34:36], "big"),
        ad[36],
        kek.decode("ascii"),
    )


KEK_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._:-"


def test_text_ads_round_trip_through_an_independent_decoder() -> None:
    rng = random.Random(20261116)  # noqa: S311 - test data only, not key material
    slots = sorted(EVER_ASSIGNED_SLOTS.keys() - f.RETIRED_SLOT_CODES)
    seen: dict[bytes, tuple[Any, ...]] = {}
    for _ in range(10_000):
        thesis, obj = uuid.UUID(int=rng.getrandbits(128)), uuid.UUID(int=rng.getrandbits(128))
        slot, level = rng.choice(slots), rng.choice(list(L))
        kek_id = "".join(rng.choice(KEK_ALPHABET) for _ in range(rng.randint(1, 64)))
        ad = f.text_ad(thesis_id=thesis, object_id=obj, slot=slot, level=level)
        expected = (thesis, obj, slot, EXPECTED_LEVEL_CODES[level])
        assert _reference_decode_text_ad(ad) == expected
        assert seen.setdefault(ad, expected) == expected
        wrap = f.text_dek_wrap_ad(
            thesis_id=thesis, object_id=obj, slot=slot, level=level, kek_id=kek_id
        )
        assert _reference_decode_text_wrap_ad(wrap) == (*expected, kek_id)


# --------------------------------------------------------------------------- padding


def _reference_pad(data: bytes) -> bytes:
    """ISO/IEC 7816-4 to a multiple of 64, written from the definition."""
    padded = data + b"\x80"
    while len(padded) % 64:
        padded += b"\x00"
    return padded


@pytest.mark.parametrize(
    ("length", "padded_length"),
    [(0, 64), (1, 64), (5, 64), (60, 64), (63, 64), (64, 128), (127, 128), (128, 192)],
)
def test_padding_rounds_up_to_the_next_multiple_of_64(length: int, padded_length: int) -> None:
    data = bytes(range(1, length + 1)) if length < 256 else b"a" * length
    padded = f.pad_text(data)
    assert len(padded) == padded_length
    assert padded == _reference_pad(data)
    assert f.unpad_text(padded) == data


def test_padding_round_trips_including_data_ending_in_zeros_or_0x80() -> None:
    rng = random.Random(20261117)  # noqa: S311 - test data only
    samples = [b"\x00", b"a\x00\x00", b"\x80", b"\x80\x00", b"\x80" * 63, b"\x00" * 64]
    samples += [rng.randbytes(rng.randint(0, 300)) for _ in range(2_000)]
    for data in samples:
        padded = f.pad_text(data)
        assert padded == _reference_pad(data)
        assert f.unpad_text(padded) == data


def test_the_largest_text_fills_the_largest_padded_size() -> None:
    assert f.PAD_BLOCK == 64
    assert f.MAX_TEXT_BYTES == 16_383
    assert len(f.pad_text(b"x" * f.MAX_TEXT_BYTES)) == f.MAX_PADDED_TEXT_BYTES == 16_384
    with pytest.raises(ValueError, match="too long"):
        f.pad_text(b"x" * (f.MAX_TEXT_BYTES + 1))


@pytest.mark.parametrize(
    "padded",
    [
        b"",  # empty
        b"abc\x80",  # not a multiple of 64
        b"a" * 64,  # no 0x80 marker
        b"\x00" * 64,  # only zeros
        b"abc" + b"\x81" + b"\x00" * 60,  # wrong marker
        b"abc\x80" + b"\x00" * 59 + b"\x01",  # garbage after the marker
        b"abc\x80" + b"\x00" * 124,  # marker more than one block before the end
        b"\x80" + b"\x00" * 127,  # padding of 128 bytes: never produced
        b"x" * 16_384 + b"\x80" + b"\x00" * 63,  # beyond the largest padded size
    ],
)
def test_malformed_padding_is_an_integrity_failure(padded: bytes) -> None:
    with pytest.raises(VaultIntegrityError) as caught:
        f.unpad_text(padded)
    assert str(caught.value) == INTEGRITY_MESSAGE
    assert caught.value.__cause__ is None and caught.value.__context__ is None


def test_unpad_refuses_what_is_not_bytes() -> None:
    with pytest.raises(VaultIntegrityError):
        f.unpad_text("abc\x80" + "\x00" * 60)  # type: ignore[arg-type]
