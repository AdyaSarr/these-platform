"""Design tokens (static/css/base.css): WCAG 2.2 AA contrast of every pair the
components use, in the light and in the dark theme.

The dark values are written twice: under prefers-color-scheme (when the viewer
chose nothing, or "auto") and under [data-theme="dark"] (the toggle). Both blocks
must be identical, and every colour token must have a dark value.
"""

from __future__ import annotations

import re
from functools import cache
from pathlib import Path

import pytest

from apps.objectives.tests.test_chart_contrast import contrast

CSS = (Path(__file__).resolve().parent.parent / "static" / "css" / "base.css").read_text()
HEX = re.compile(r"(--[\w-]+)\s*:\s*(#[0-9a-fA-F]{6})\s*;")


def _block(selector: str) -> dict[str, str]:
    match = re.search(re.escape(selector) + r"\s*\{([^{}]*)\}", CSS)
    assert match is not None, selector
    return dict(HEX.findall(match.group(1)))


@cache
def _themes() -> tuple[dict[str, str], dict[str, str], dict[str, str]]:
    """(light, dark under prefers-color-scheme, dark chosen with the toggle)."""
    return (
        _block(":root"),
        _block(':root:not([data-theme="light"])'),
        _block(':root[data-theme="dark"]'),
    )


def test_the_auto_dark_block_sits_in_the_media_query() -> None:
    assert re.search(
        r'@media \(prefers-color-scheme: dark\)\s*\{\s*:root:not\(\[data-theme="light"\]\)', CSS
    )


def test_both_dark_blocks_are_identical() -> None:
    _, auto, chosen = _themes()
    assert auto == chosen


def test_every_colour_token_has_a_dark_value() -> None:
    light, auto, _ = _themes()
    assert set(light) == set(auto)


def test_colour_scheme_follows_the_choice() -> None:
    assert re.search(r":root\s*\{[^}]*color-scheme:\s*light dark", CSS)
    assert re.search(r':root\[data-theme="light"\]\s*\{[^}]*color-scheme:\s*light;', CSS)
    assert re.search(r':root\[data-theme="dark"\]\s*\{[^}]*color-scheme:\s*dark;', CSS)


TEXT = 4.5  # 1.4.3
UI = 3.0  # 1.4.11: component boundaries, focus indicator, graphics

SURFACES = ["--bg", "--surface", "--surface-2"]
PAIRS: list[tuple[str, str, float]] = [
    *[(fg, bg, TEXT) for fg in ("--fg", "--muted", "--link", "--error") for bg in SURFACES],
    *[(fg, "--sidebar-bg", TEXT) for fg in ("--nav-fg", "--muted")],
    ("--accent-strong", "--accent-soft", TEXT),  # active navigation item
    ("--accent-strong", "--bg", TEXT),  # current tab
    ("--accent-strong", "--surface", TEXT),  # current segment
    ("--link", "--accent-soft", TEXT),  # discreet button, hovered
    ("--accent-2-strong", "--accent-2-soft", TEXT),  # secondary badge
    ("--accent-2-strong", "--surface", TEXT),
    ("--accent-fg", "--accent", TEXT),  # primary button
    ("--accent-fg", "--accent-hover", TEXT),
    ("--danger-solid-fg", "--danger-solid", TEXT),  # danger button
    ("--danger-solid-fg", "--danger-solid-hover", TEXT),
    *[(f"--{s}-fg", f"--{s}-bg", TEXT) for s in ("success", "warning", "danger", "info")],
    *[(f"--{s}-fg", "--surface", TEXT) for s in ("success", "warning", "danger", "info")],
    ("--overdue", "--bg", TEXT),
    ("--overdue", "--surface", TEXT),
    ("--restricted-fg", "--restricted-bg", TEXT),
    *[("--border-strong", bg, UI) for bg in SURFACES],  # form field boundaries
    *[("--focus", bg, UI) for bg in [*SURFACES, "--sidebar-bg", "--accent-soft"]],
    *[("--accent", bg, UI) for bg in SURFACES],  # button against the page
    *[("--danger-solid", bg, UI) for bg in SURFACES],
    ("--progress-fill", "--progress-track", UI),
    ("--progress-fill", "--surface", UI),
    ("--chart-line", "--surface", UI),
]


@pytest.mark.parametrize("theme", ["light", "dark"])
@pytest.mark.parametrize(("fg", "bg", "minimum"), PAIRS, ids=lambda x: str(x))
def test_contrast(theme: str, fg: str, bg: str, minimum: float) -> None:
    light, auto, _ = _themes()
    colours = light if theme == "light" else auto
    assert fg in colours and bg in colours, (fg, bg)
    ratio = contrast(colours[fg], colours[bg])
    assert ratio >= minimum, f"{theme}: {fg} on {bg} = {ratio:.2f} < {minimum}"
