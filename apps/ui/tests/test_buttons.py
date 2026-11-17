"""Hierarchy and spacing of the buttons (review B of the redesign).

- One filled (primary) button per form, and per block: a plain <button> is
  secondary (outlined); only class="btn-primary" fills it. The page crawl of the
  demonstration thesis checks every block of every page for every role
  (tests/test_pages_per_role.py).
- A form that collects data (a field, a form rendered by Django) has exactly one
  primary button: its submit.
- Buttons keep their natural width; a filter is one line (field and button);
  sections and forms keep a regular vertical rhythm.
- « Délier » is discreet (a link to undo, not a main action).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

BASE_DIR = Path(__file__).resolve().parents[3]
TEMPLATES = BASE_DIR / "templates"

FORM = re.compile(r"<form\b.*?</form>", re.S)
PRIMARY = re.compile(r'<button\b[^>]*class="[^"]*\bbtn-primary\b')
# A form collecting data: a visible field, or a form rendered by Django.
COLLECTS = re.compile(
    r'<(?:textarea|select)\b|<input\b(?![^>]*type="hidden")|\{\{\s*[\w.]*form[\w.]*(?:\.as_\w+)?\s*\}\}'
    r'|\{\{\s*field\s*\}\}|\{%\s*include\s+"[^"]*_form\.html"|\{\{\s*[\w.]+\.(?:due_date|role|days)\s*\}\}'
)


def _forms() -> list[tuple[str, str]]:
    return [
        (str(p.relative_to(TEMPLATES)), form)
        for p in sorted(TEMPLATES.rglob("*.html"))
        for form in FORM.findall(p.read_text(encoding="utf-8"))
    ]


def test_at_most_one_primary_button_per_form() -> None:
    found = [(name, form[:80]) for name, form in _forms() if len(PRIMARY.findall(form)) > 1]
    assert found == []


# Forms that collect data without a main submit of their own: a filter (its
# button is secondary: it only narrows a list), the theme toggle, the small forms
# of a member card's menu (secondary actions side by side, one block).
# A form marked form-secondary is a side action of its block (a due date next to
# the deposit, a row of a table): its submit stays secondary.
NOT_A_SUBMISSION = ("Filter", "theme-toggle", 'class="menu-form"', "form-secondary")
# The main submit: primary, or danger when the form's action is (reject...).
MAIN = re.compile(r'<button\b[^>]*class="[^"]*\b(?:btn-primary|danger|btn-danger)\b')


def test_a_form_that_collects_data_has_one_primary_submit() -> None:
    found = [
        (name, form[:100])
        for name, form in _forms()
        if COLLECTS.search(form)
        and not any(marker in form for marker in NOT_A_SUBMISSION)
        and len(MAIN.findall(form)) != 1
    ]
    assert found == []


def test_filters_are_one_line() -> None:
    filters = [(name, form) for name, form in _forms() if '{% translate "Filter" %}' in form]
    assert filters
    for name, form in filters:
        assert re.match(r'<form class="filter-row"', form), name


def test_unlinking_is_discreet() -> None:
    unlinks = [(name, form) for name, form in _forms() if "unlink_document" in form]
    assert len(unlinks) == 2
    for name, form in unlinks:
        assert re.search(r'<button type="submit" class="btn-ghost btn-sm">', form), name


def _css() -> str:
    css = (BASE_DIR / "static" / "css" / "components.css").read_text(encoding="utf-8")
    return re.sub(r"/\*.*?\*/", "", css, flags=re.S)


def _rule(selector: str) -> str:
    rules = {sel.strip(): body for sel, body in re.findall(r"([^{}]+)\{([^{}]*)\}", _css())}
    return str(rules.get(selector, ""))


def test_a_plain_button_is_secondary() -> None:
    plain = _rule("button")
    assert "var(--accent)" not in plain and re.search(r"background\s*:\s*var\(--surface\)", plain)
    assert re.search(r"background\s*:\s*var\(--accent\)", _rule("button.btn-primary, a.btn"))


@pytest.mark.parametrize(
    ("selector", "declaration"),
    [
        ("form button", r"justify-self\s*:\s*start"),
        (".filter-row", r"display\s*:\s*flex"),
        (".content h2", r"margin-block-start\s*:\s*var\(--space-6\)"),
        ("form", r"margin-block-end\s*:\s*var\(--space-4\)"),
    ],
)
def test_spacing_rules(selector: str, declaration: str) -> None:
    assert re.search(declaration, _rule(selector)), selector
