"""formats.canonical_json: the RFC 8785 (JCS) subset hashed by the audit chain."""

import json
from typing import Any

import pytest

from apps.vault.formats import canonical_json


def test_exact_bytes() -> None:
    value = {"b": [1, True, None, "x"], "a": {"d": -3, "c": ""}, "seq": 2**53 - 1}
    assert canonical_json(value) == (
        b'{"a":{"c":"","d":-3},"b":[1,true,null,"x"],"seq":9007199254740991}'
    )


def test_rfc8785_string_escapes() -> None:
    """RFC 8785, 3.2.2.2: only " \\ and C0 controls are escaped (short forms
    \\b \\t \\n \\f \\r, else \\u00xx in lower case); "/" and non-ASCII are not.

    Input of the RFC example, by code point: EURO SIGN, "$", U+000F, U+000A, "A",
    "'", "B", '"', two backslashes, '"', "/".
    """
    value = "".join(
        map(chr, [0x20AC, 0x24, 0x0F, 0x0A, 0x41, 0x27, 0x42, 0x22, 0x5C, 0x5C, 0x22, 0x2F])
    )
    backslash, quote = chr(0x5C), chr(0x22)
    expected = (
        quote
        + chr(0x20AC)
        + "$"
        + backslash
        + "u000f"
        + backslash
        + "n"
        + "A'B"
        + backslash
        + quote
        + backslash
        + backslash
        + backslash
        + backslash
        + backslash
        + quote
        + "/"
        + quote
    )
    assert canonical_json(value) == expected.encode("utf-8")


@pytest.mark.parametrize(
    ("char", "escaped"),
    [
        ("\b", "\\b"),
        ("\t", "\\t"),
        ("\n", "\\n"),
        ("\f", "\\f"),
        ("\r", "\\r"),
        ("\x00", "\\u0000"),
        ("\x1f", "\\u001f"),
        ("\x7f", "\x7f"),
        ("\u2028", "\u2028"),  # LINE SEPARATOR: not escaped by RFC 8785
    ],
)
def test_control_characters(char: str, escaped: str) -> None:
    assert canonical_json(char) == f'"{escaped}"'.encode()


def test_rfc8785_key_order_is_by_utf16_code_units() -> None:
    """RFC 8785, 3.2.3: an emoji (surrogate pair D83D...) sorts BEFORE U+FB33,
    unlike Python's code-point order."""
    value = {
        "€": "Euro Sign",
        "\r": "Carriage Return",
        "דּ": "Hebrew Letter Dalet With Dagesh",
        "1": "One",
        "\U0001f600": "Emoji: Grinning Face",
        "\u0080": "Control",
        "ö": "Latin Small Letter O With Diaeresis",
    }
    keys = list(json.loads(canonical_json(value)))
    assert keys == ["\r", "1", "\u0080", "ö", "€", "\U0001f600", "דּ"]
    assert sorted(value) != keys  # code-point order differs


def test_output_is_utf8_and_round_trips() -> None:
    value = {"thèse": ["état", "€"]}
    raw = canonical_json(value)
    assert raw.decode("utf-8") == '{"thèse":["état","€"]}'
    assert json.loads(raw) == value


@pytest.mark.parametrize(
    "value",
    [1.0, float("nan"), 2**53, -(2**53), {"a": 0.5}, (1, 2), {1: "int key"}, b"bytes", object()],
    ids=["float", "nan", "2^53", "-2^53", "nested-float", "tuple", "int-key", "bytes", "object"],
)
def test_values_outside_the_subset_are_refused(value: Any) -> None:
    with pytest.raises(ValueError):
        canonical_json(value)


@pytest.mark.parametrize("text", ["a\ud800b", "\udfff", "\ud83d"])
def test_lone_surrogates_are_refused(text: str) -> None:
    with pytest.raises(ValueError):
        canonical_json(text)
    with pytest.raises(ValueError):
        canonical_json({text: 1})


def test_valid_surrogate_pairs_are_characters() -> None:
    assert canonical_json("\U0001f600") == '"\U0001f600"'.encode()
