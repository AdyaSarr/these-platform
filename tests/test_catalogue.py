"""The French catalogue as a whole: no fuzzy entry (msgmerge's guesses, never compiled
and sometimes plainly wrong) and no empty translation, long multi-line messages
included."""

from __future__ import annotations

import re
from pathlib import Path

PO = Path(__file__).resolve().parent.parent / "locale" / "fr" / "LC_MESSAGES" / "django.po"


def _entries() -> list[str]:
    blocks = PO.read_text().split("\n\n")[1:]  # the first block is the header
    return [b for b in blocks if not b.lstrip().startswith("#~") and "\nmsgid " in "\n" + b]


def _joined(block: str, keyword: str) -> str:
    """The full string of ``msgid`` / ``msgstr`` (or msgstr[n]), continuation lines included."""
    match = re.search(rf'^{re.escape(keyword)} ((?:".*"\n?)+)', block, re.M)
    return "".join(re.findall(r'"(.*)"', match.group(1))) if match else ""


def test_no_fuzzy_entry() -> None:
    fuzzy = [_joined(b, "msgid")[:80] for b in _entries() if re.search(r"^#,.*\bfuzzy\b", b, re.M)]
    assert fuzzy == []


def test_no_empty_translation() -> None:
    empty = []
    for block in _entries():
        keywords = re.findall(r"^(msgstr(?:\[\d\])?) ", block, re.M)
        if any(_joined(block, k) == "" for k in keywords):
            empty.append(_joined(block, "msgid")[:80])
    assert empty == []


def test_the_parser_sees_multi_line_entries() -> None:
    """Guards the two tests above against a parser that silently skips long messages."""
    long_ones = [b for b in _entries() if re.search(r'^msgid ""\n"', b, re.M)]
    assert len(long_ones) > 5
    assert all(_joined(b, "msgid") for b in long_ones)
