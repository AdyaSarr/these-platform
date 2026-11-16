"""French typography (frontend, step 2 point 14): a no-break space (U+00A0) or a
narrow no-break space (U+202F) before ; : ? ! % €, never an ordinary one; « »
quotes, never straight or English curly ones.

Checked three times: in the text of every template (pages and e-mails), in the
French translation of every string a template translates, and in every msgstr
of the French catalogue (which also holds the strings of the Python code).
"""

from __future__ import annotations

import re
from functools import cache
from pathlib import Path

import pytest
from django.utils import translation

BASE_DIR = Path(__file__).resolve().parent.parent
TEMPLATES = BASE_DIR / "templates"

CATALOGUE = BASE_DIR / "locale" / "fr" / "LC_MESSAGES" / "django.po"

# Every template: the pages and the plain-text e-mails.
ALL_TEMPLATES = sorted(
    str(p.relative_to(TEMPLATES))
    for p in TEMPLATES.rglob("*")
    if p.is_file() and p.suffix in {".html", ".txt"}
)

# An ordinary space before a high punctuation sign; "%(name)s" is a placeholder.
LOOSE_SPACE = re.compile(r"[ \t\n](?:[;:?!€]|%(?!\())")
BAD_QUOTES = re.compile(r"[\"“”]")


def _text_of(source: str) -> str:
    """What a reader sees: no tags, no template syntax, no comments."""
    source = re.sub(r"\{%\s*comment\s*%\}.*?\{%\s*endcomment\s*%\}", "", source, flags=re.S)
    source = re.sub(r"\{#.*?#\}", "", source, flags=re.S)
    # The English msgid of a blocktranslate is not shown: its French is checked below.
    source = re.sub(
        r"\{%\s*blocktranslate\b.*?\{%\s*endblocktranslate\s*%\}", "", source, flags=re.S
    )
    source = re.sub(r"\{%.*?%\}|\{\{.*?\}\}", "", source, flags=re.S)
    return re.sub(r"<[^>]*>", "", source, flags=re.S)


def _msgids(source: str) -> list[tuple[str, str | None]]:
    """(singular, plural) of every string the template translates."""
    found: list[tuple[str, str | None]] = [
        (m, None) for m in re.findall(r"""\{%\s*translate\s+"([^"]+)"[^%]*%\}""", source)
    ]
    for body in re.findall(
        r"\{%\s*blocktranslate\b[^%]*%\}(.*?)\{%\s*endblocktranslate\s*%\}", source, flags=re.S
    ):
        parts = re.split(r"\{%\s*plural\s*%\}", body)
        as_msgid = [re.sub(r"\{\{\s*(\w+)\s*\}\}", r"%(\1)s", p) for p in parts]
        found.append((as_msgid[0], as_msgid[1] if len(as_msgid) > 1 else None))
    return found


def _french(singular: str, plural: str | None) -> list[str]:
    with translation.override("fr"):
        if plural is None:
            return [translation.gettext(singular)]
        return [translation.ngettext(singular, plural, n) for n in (1, 2)]


@pytest.mark.parametrize("name", ALL_TEMPLATES)
def test_template_text(name: str) -> None:
    text = _text_of((TEMPLATES / name).read_text(encoding="utf-8"))
    assert not LOOSE_SPACE.findall(text), (name, LOOSE_SPACE.findall(text))
    assert not BAD_QUOTES.findall(text), name


@pytest.mark.parametrize("name", ALL_TEMPLATES)
def test_translations(name: str) -> None:
    source = (TEMPLATES / name).read_text(encoding="utf-8")
    for singular, plural in _msgids(source):
        for french in _french(singular, plural):
            assert not LOOSE_SPACE.search(french), (name, french)
            assert not BAD_QUOTES.search(french), (name, french)


def test_the_typography_scan_sees_what_it_must() -> None:
    assert LOOSE_SPACE.search("Taux : 3")
    assert LOOSE_SPACE.search("40 %")
    assert not LOOSE_SPACE.search("Taux\u00a0: 40\u202f%")
    assert not LOOSE_SPACE.search("sur %(total)s pièces")
    assert _text_of('<a href="x">{% translate "A" %}</a> {{ x }} :') == "  :"
    plural = (
        "{% blocktranslate count counter=n %}{{ counter }} a"
        "{% plural %}{{ counter }} b{% endblocktranslate %}"
    )
    assert _msgids(plural) == [("%(counter)s a", "%(counter)s b")]


def test_every_template_is_checked() -> None:
    assert len(ALL_TEMPLATES) > 90
    assert any(name.endswith(".txt") for name in ALL_TEMPLATES)  # the e-mails too


def _unquote(line: str) -> str:
    body = line[line.index('"') + 1 : line.rindex('"')]
    return body.replace('\\"', '"').replace("\\n", "\n").replace("\\\\", "\\")


@cache
def _catalogue() -> list[tuple[str, str]]:
    """(msgid, msgstr) of every translated, non-fuzzy entry; plural forms apart."""
    entries: list[tuple[str, str]] = []
    for block in CATALOGUE.read_text(encoding="utf-8").split("\n\n"):
        lines = [line for line in block.splitlines() if not line.startswith("#~")]
        if any(line.startswith("#,") and "fuzzy" in line for line in lines):
            continue
        fields: dict[str, str] = {}
        current = ""
        for line in lines:
            if line.startswith("#"):
                continue
            if line.startswith('"'):
                fields[current] += _unquote(line)
                continue
            key = line.split(" ", 1)[0]
            current = key
            fields[key] = _unquote(line)
        msgid = fields.get("msgid", "")
        if not msgid:
            continue  # the header
        for key, value in fields.items():
            if key.startswith("msgstr") and value:
                entries.append((msgid, value))
    return entries


def test_the_catalogue_is_read() -> None:
    entries = dict(_catalogue())
    assert len(entries) > 500
    assert entries["Coming up"] == "À venir"


@pytest.mark.parametrize("msgid", sorted({m for m, _ in _catalogue()}))
def test_catalogue(msgid: str) -> None:
    for _, french in (e for e in _catalogue() if e[0] == msgid):
        assert not LOOSE_SPACE.search(french), (msgid, french)
        assert not BAD_QUOTES.search(french), (msgid, french)
