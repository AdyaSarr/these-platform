"""The common iCalendar writer (apps/core/ics.py, M12): RFC 5545 escaping, folding
at 75 octets, and the refusal of any control character in a value (CR, LF
included) before escaping. The exports of M5a and M7 keep using it unchanged."""

from __future__ import annotations

import pytest

from apps.core import ics


def test_escape() -> None:
    assert ics.escape("a\\b;c,d") == "a\\\\b\\;c\\,d"


def test_fold_at_75_octets_never_inside_a_character() -> None:
    line = "SUMMARY:" + "é" * 100
    folded = ics.fold(line)
    for part in folded.split("\r\n"):
        assert len(part.encode()) <= 75
    assert folded.replace("\r\n ", "") == line


@pytest.mark.parametrize("bad", ["a\nb", "a\rb", "a\r\nb", "a\x00b", "a\x1bb", "a\x7fb"])
def test_a_control_character_is_refused(bad: str) -> None:
    with pytest.raises(ValueError):
        ics.text(bad)


def test_the_modules_use_the_common_writer() -> None:
    from apps.meetings import ics as meetings_ics
    from apps.travel import ics as travel_ics

    assert meetings_ics.escape is ics.escape and meetings_ics.fold is ics.fold
    assert travel_ics.escape is ics.escape and travel_ics.fold is ics.fold
