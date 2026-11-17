"""The common iCalendar writer (RFC 5545): the exports of M5a and M7 and the
personal feed of M12 write their own lines with it; no parser or library.

``text(value)`` refuses any control character (CR, LF included) BEFORE escaping:
a value is always one line of our own, never a way to add a property.
"""

from __future__ import annotations

import datetime as dt
import unicodedata

CRLF = "\r\n"
MAX_OCTETS = 75


def escape(text: str) -> str:
    """TEXT value (RFC 5545 §3.3.11): backslash, semicolon, comma, newline."""
    return (
        text.replace("\\", "\\\\")
        .replace(";", r"\;")
        .replace(",", "\\,")
        .replace("\r\n", "\\n")
        .replace("\n", "\\n")
    )


def text(value: str) -> str:
    """A TEXT value that holds no control character, escaped (M12)."""
    if any(unicodedata.category(c) == "Cc" for c in value):
        raise ValueError("a control character in an iCalendar value")
    return escape(value)


def fold(line: str) -> str:
    """Fold at 75 octets (RFC 5545 §3.1), never inside a UTF-8 sequence."""
    out: list[str] = []
    current, size, limit = "", 0, MAX_OCTETS
    for char in line:
        width = len(char.encode())
        if size + width > limit:
            out.append(current)
            current, size, limit = "", 0, MAX_OCTETS - 1  # the leading space counts
        current += char
        size += width
    out.append(current)
    return (CRLF + " ").join(out)


def utc(instant: dt.datetime) -> str:
    return instant.astimezone(dt.UTC).strftime("%Y%m%dT%H%M%SZ")
