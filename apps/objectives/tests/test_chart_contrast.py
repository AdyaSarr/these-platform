"""Colours of the chart and of the objective states (static/css/base.css): WCAG AA
contrast in both themes, and no information carried by colour alone."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

CSS = (Path(__file__).resolve().parents[3] / "static" / "css" / "base.css").read_text()


def _variables(block: str) -> dict[str, str]:
    return dict(re.findall(r"(--[\w-]+)\s*:\s*(#[0-9a-fA-F]{6})", block))


def _themes() -> dict[str, dict[str, str]]:
    light = _variables(CSS.split("@media (prefers-color-scheme: dark)")[0])
    dark_block = re.search(r"@media \(prefers-color-scheme: dark\)\s*\{(.*?)\n\}", CSS, re.S)
    assert dark_block is not None
    return {"light": light, "dark": {**light, **_variables(dark_block.group(1))}}


def _luminance(hex_colour: str) -> float:
    def channel(c: int) -> float:
        s = c / 255
        return s / 12.92 if s <= 0.04045 else ((s + 0.055) / 1.055) ** 2.4

    r, g, b = (int(hex_colour[i : i + 2], 16) for i in (1, 3, 5))
    return 0.2126 * channel(r) + 0.7152 * channel(g) + 0.0722 * channel(b)


def contrast(a: str, b: str) -> float:
    la, lb = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


def test_the_formula_matches_known_values() -> None:
    assert contrast("#000000", "#ffffff") == pytest.approx(21.0)
    assert contrast("#767676", "#ffffff") == pytest.approx(4.54, abs=0.01)


# (foreground, background, minimum): text 4.5:1 (1.4.3), graphics 3:1 (1.4.11).
PAIRS = [
    ("--fg", "--bg", 4.5),
    ("--muted", "--bg", 4.5),  # tick labels, secondary text
    ("--chart-line", "--bg", 3.0),
    ("--chart-dot", "--bg", 3.0),
    ("--warning-fg", "--warning-bg", 4.5),
    ("--overdue", "--bg", 4.5),
    ("--restricted-fg", "--restricted-bg", 4.5),
]


@pytest.mark.parametrize("theme", ["light", "dark"])
@pytest.mark.parametrize(("fg", "bg", "minimum"), PAIRS)
def test_contrast(theme: str, fg: str, bg: str, minimum: float) -> None:
    colours = _themes()[theme]
    assert fg in colours and bg in colours, (fg, bg)
    assert contrast(colours[fg], colours[bg]) >= minimum, (theme, fg, bg)


def test_states_are_not_told_by_colour_alone() -> None:
    """The line is solid with dot markers, the grid dashed; overdue and restricted
    items carry a word (templates), and the chart has a data table."""
    assert re.search(r"\.chart-grid\s*\{[^}]*stroke-dasharray", CSS)
    assert re.search(r"\.chart-dot\s*\{", CSS)
    templates = Path(__file__).resolve().parents[3] / "templates" / "objectives"
    text = "".join(p.read_text() for p in templates.glob("*.html"))
    assert 'class="overdue"' in text and '{% translate "overdue" %}' in text
    assert '{% translate "Restricted content" %}' in text
