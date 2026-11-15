"""Euro amounts of the travel module (pure functions, no database).

Typed by hand (``parse``): ASCII digits only (Python's \\d also takes full-width and
other scripts' digits), a comma or a point and at most two decimals, no sign, no
thousands separator, no exponent; at most 999 999.99 EUR. Held as integer cents:
no float ever. Sealed as one canonical text (``canonical``: "1234.50"), read back
strictly (``from_canonical``): anything else in a sealed amount is corruption.
"""

from __future__ import annotations

import re

MAX_CENTS = 99_999_999  # 999 999.99 EUR
NBSP = "\u00a0"  # no-break space: the euro sign never goes alone to the next line

_TYPED = re.compile(r"([0-9]{1,6})(?:[.,]([0-9]{1,2}))?")
_CANONICAL = re.compile(r"(0|[1-9][0-9]{0,5})\.([0-9]{2})")


def _cents(value: int) -> int:
    if type(value) is not int:  # bool and float refused
        raise TypeError("an amount is an int number of cents")
    if not 0 <= value <= MAX_CENTS:
        raise ValueError("an amount is within 0 .. 999 999.99")
    return value


def parse(raw: str) -> int:
    """Cents of an amount typed by hand; ValueError when it is not one."""
    if not isinstance(raw, str):
        raise TypeError("an amount is typed as text")
    match = _TYPED.fullmatch(raw.strip())
    if match is None:
        raise ValueError("not an amount")
    units, decimals = match.group(1), match.group(2) or ""
    return _cents(int(units) * 100 + int(decimals.ljust(2, "0") or "0"))


def canonical(cents: int) -> str:
    """The sealed form: "1234.50"."""
    units, rest = divmod(_cents(cents), 100)
    return f"{units}.{rest:02d}"


def from_canonical(text: str) -> int:
    match = _CANONICAL.fullmatch(text)
    if match is None:
        raise ValueError("not a canonical amount")
    return _cents(int(match.group(1)) * 100 + int(match.group(2)))


def display(cents: int) -> str:
    """French display: "1 234,50 €" with no-break spaces."""
    units, rest = divmod(_cents(cents), 100)
    grouped = f"{units:,}".replace(",", NBSP)
    return f"{grouped},{rest:02d}{NBSP}€"
