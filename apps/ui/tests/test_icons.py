"""Lucide icons (ISC), vendored in static/vendor/lucide/, inlined by {% icon %}:
decorative (hidden from assistive technologies), clean under the strict CSP."""

from __future__ import annotations

from pathlib import Path

import pytest
from django.template import Context, Template

BASE_DIR = Path(__file__).resolve().parents[3]
LUCIDE = BASE_DIR / "static" / "vendor" / "lucide"


def _names() -> tuple[str, ...]:
    from apps.ui.icons import ICON_NAMES

    return ICON_NAMES


def test_every_vendored_icon_is_used_and_every_name_has_a_file() -> None:
    files = {p.stem for p in LUCIDE.glob("*.svg")}
    assert files == set(_names())


def test_icon_markup() -> None:
    for name in _names():
        html = Template('{% load ui %}{% icon "' + name + '" %}').render(Context())
        assert html.startswith('<svg class="icon" aria-hidden="true" focusable="false"'), name
        assert html.rstrip().endswith("</svg>"), name
        lowered = html.lower()
        for forbidden in ("style", "<script", " on", "href", "<!--", "lucide"):
            assert forbidden not in lowered, (name, forbidden)


def test_unknown_icon_fails_loudly() -> None:
    with pytest.raises(ValueError):
        Template('{% load ui %}{% icon "no-such-icon" %}').render(Context())


def test_an_svg_with_a_style_is_refused(tmp_path: Path) -> None:
    from apps.ui.icons import clean_svg

    with pytest.raises(ValueError):
        clean_svg('<svg viewBox="0 0 24 24" style="x"><path d="M0 0"/></svg>')
    with pytest.raises(ValueError):
        clean_svg('<svg viewBox="0 0 24 24"><script>alert(1)</script></svg>')
    with pytest.raises(ValueError):
        clean_svg(
            '<svg viewBox="0 0 24 24"><a href="https://evil.example/"><path d="M0 0"/></a></svg>'
        )
