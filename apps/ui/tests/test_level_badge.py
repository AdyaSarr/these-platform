"""One badge for a confidentiality level, everywhere it is shown (documents,
meetings, objectives, submissions, missions): the word, a tone and an icon of
its own per level, so that Interne ST and Interne LIRMM are told apart at a
glance; never the colour alone (the word is always there)."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from django.template import Context, Template
from django.utils import translation

from apps.core.confidentiality import ConfidentialityLevel as L

BASE_DIR = Path(__file__).resolve().parents[3]
LABELS = {
    L.PUBLIC: "Public",
    L.INTERNAL_LIRMM: "Interne LIRMM",
    L.INTERNAL_ST: "Interne ST",
    L.CONFIDENTIAL: "Confidentiel",
}


def _badge(level: object) -> str:
    with translation.override("fr"):
        return Template("{% load ui %}{% level_badge level %}").render(Context({"level": level}))


def _icon(html: str) -> str:
    match = re.search(r"<svg .*?</svg>", html, re.S)
    assert match is not None
    return match.group(0)


@pytest.mark.parametrize("level", list(L))
def test_the_badge_has_the_word_and_its_level(level: L) -> None:
    html = _badge(level.value)
    assert re.search(
        rf'<span class="badge badge-\w+ level-badge" data-level="{level.value}">', html
    )
    text = re.sub(r"<[^>]+>", "", html).strip()
    assert text == LABELS[level]


def test_each_level_has_an_icon_of_its_own() -> None:
    icons = {level: _icon(_badge(level.value)) for level in L}
    assert len(set(icons.values())) == len(icons)


def test_tones_as_validated() -> None:
    tones = {
        level: re.search(r"badge-(\w+) level-badge", _badge(level.value)).group(1)  # type: ignore[union-attr]
        for level in L
    }
    assert tones == {
        L.PUBLIC: "neutral",
        L.INTERNAL_LIRMM: "info",
        L.INTERNAL_ST: "info",
        L.CONFIDENTIAL: "warning",
    }


def test_an_unknown_value_shows_nothing_made_up() -> None:
    assert _badge("SOMETHING") == "" and _badge(None) == ""


def _templates() -> list[Path]:
    paths = list((BASE_DIR / "templates").rglob("*.html"))
    paths += list((BASE_DIR / "apps").glob("*/templates/**/*.html"))
    return sorted(paths)


def test_every_level_shown_is_the_badge() -> None:
    raw = re.compile(r"get_(?:level|confidentiality|snapshot_level)_display")
    found = [str(p.relative_to(BASE_DIR)) for p in _templates() if raw.search(p.read_text("utf-8"))]
    assert found == []
    users = [p for p in _templates() if "{% level_badge" in p.read_text("utf-8")]
    named = {p.relative_to(BASE_DIR).as_posix() for p in users}
    for page in (
        "templates/vault/document_list.html",
        "templates/vault/document_detail.html",
        "templates/meetings/list.html",
        "templates/meetings/detail.html",
        "templates/objectives/detail.html",
        "templates/conferences/submissions.html",
        "templates/conferences/submission.html",
        "templates/travel/mission.html",
    ):
        assert page in named, page
