"""travel.amounts: euro amounts typed by hand, parsed strictly into cents and sealed in
one canonical form (pure functions, no database)."""

from __future__ import annotations

import pytest

from apps.travel import amounts


@pytest.mark.parametrize(
    ("raw", "cents"),
    [
        ("0", 0),
        ("12", 1200),
        ("12.5", 1250),
        ("12,5", 1250),
        ("12.50", 1250),
        ("12,05", 1205),
        ("999999.99", 99999999),
        ("  84,30 ", 8430),  # surrounding spaces of a form field
    ],
)
def test_parse(raw: str, cents: int) -> None:
    assert amounts.parse(raw) == cents


@pytest.mark.parametrize(
    "raw",
    [
        "",
        " ",
        "-1",
        "+1",
        "1e3",
        "12.345",
        "1 000",  # thousands separators are refused: typed amounts are short
        "1.000,50",
        "1000000",  # above 999 999.99
        "0x10",
        "\uff11\uff12",  # full-width digits: \d would accept them
        "\u0661\u0662",  # Arabic-Indic digits
        "12.",
        ".5",
        "12,,5",
        "NaN",
        "inf",
        "12€",
    ],
)
def test_parse_refuses(raw: str) -> None:
    with pytest.raises(ValueError):
        amounts.parse(raw)


@pytest.mark.parametrize("raw", [12, 12.5, None, b"12"])
def test_parse_needs_a_string(raw: object) -> None:
    with pytest.raises(TypeError):
        amounts.parse(raw)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("cents", "text"), [(0, "0.00"), (1205, "12.05"), (123450, "1234.50"), (99999999, "999999.99")]
)
def test_canonical_form_round_trips(cents: int, text: str) -> None:
    assert amounts.canonical(cents) == text
    assert amounts.from_canonical(text) == cents


@pytest.mark.parametrize("text", ["12.5", "12,50", "1234.5", " 1.00", "01.00", "-1.00", "1.000"])
def test_from_canonical_is_strict(text: str) -> None:
    """A sealed amount is always written by canonical(): anything else is corrupt."""
    with pytest.raises(ValueError):
        amounts.from_canonical(text)


@pytest.mark.parametrize("cents", [-1, 100000000, True, 1.5])
def test_canonical_refuses_out_of_range(cents: object) -> None:
    with pytest.raises((TypeError, ValueError)):
        amounts.canonical(cents)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("cents", "shown"),
    [(0, "0,00\u00a0€"), (1205, "12,05\u00a0€"), (123450, "1\u00a0234,50\u00a0€")],
)
def test_display_in_french(cents: int, shown: str) -> None:
    """No-break spaces: the euro sign never goes alone to the next line."""
    assert amounts.display(cents) == shown
