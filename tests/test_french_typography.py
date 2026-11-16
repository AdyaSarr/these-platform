"""French typography of the redesigned templates (frontend): a no-break space
(U+00A0) or a narrow no-break space (U+202F) before ; : ? ! % €, never an ordinary
one; « » quotes, never straight or English curly ones.

Checked twice: in the text of the templates themselves, and in the French
translation of every string they translate. The list grows with step 2 of the
redesign, module by module, until it covers every template.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from django.utils import translation

BASE_DIR = Path(__file__).resolve().parent.parent
TEMPLATES = BASE_DIR / "templates"

REDESIGNED = [
    "base.html",
    "core/home.html",
    "accounts/dashboard.html",
    "accounts/blocks/team.html",
    "admin_file/blocks/dashboard.html",
    "objectives/blocks/dashboard.html",
    "meetings/blocks/dashboard.html",
    "conferences/blocks/dashboard.html",
    "travel/blocks/dashboard.html",
    "vault/blocks/dashboard.html",
    *sorted(str(p.relative_to(TEMPLATES)) for p in (TEMPLATES / "ui").rglob("*.html")),
]

# Texts asserted verbatim, ordinary space included, by an existing test: kept as
# they are (CLAUDE.md: existing assertions unchanged), to settle with the developer
# in step 2. template -> exact source fragments, each with the test that asserts it.
KEPT: dict[str, list[str]] = {
    "accounts/dashboard.html": [
        # apps/admin_file/tests/test_setup_explanation.py::test_dates_in_french
        '{% translate "CIFRE effective date" %} :',
        '{% translate "First enrolment" %} :',
    ],
}

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


@pytest.mark.parametrize("name", REDESIGNED)
def test_template_text(name: str) -> None:
    source = (TEMPLATES / name).read_text(encoding="utf-8")
    for fragment in KEPT.get(name, []):
        assert fragment in source, (name, fragment)  # an exception that is gone: remove it
        source = source.replace(fragment, "")
    text = _text_of(source)
    assert not LOOSE_SPACE.findall(text), (name, LOOSE_SPACE.findall(text))
    assert not BAD_QUOTES.findall(text), name


@pytest.mark.parametrize("name", REDESIGNED)
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


def test_every_redesigned_template_exists() -> None:
    assert len(REDESIGNED) > 12  # the ui/ components are there
    for name in REDESIGNED:
        assert (TEMPLATES / name).is_file(), name
