"""Lucide icons (ISC licence), vendored unmodified in static/vendor/lucide/.

Inlined in the page (an <img> would be a request per icon and could not take the
text colour): the file is checked before it is trusted, then rewritten as a
decorative SVG (aria-hidden, never focusable) sized by CSS (.icon). Only the names
of ICON_NAMES exist; an unknown name raises, so a typo fails a test, not silently.
"""

from __future__ import annotations

import re
from functools import cache
from pathlib import Path

from django.conf import settings
from django.utils.safestring import SafeString, mark_safe

LUCIDE = Path(settings.BASE_DIR) / "static" / "vendor" / "lucide"

ICON_NAMES: tuple[str, ...] = (
    # Navigation
    "house", "folder-open", "target", "calendar-days", "presentation", "plane",
    "file-lock", "users", "clipboard-list", "graduation-cap",
    # Header
    "log-out", "sun", "moon", "monitor", "menu", "x",
    # States and components
    "circle-check", "triangle-alert", "octagon-alert", "info", "circle-dot", "clock",
    "inbox", "bell", "chevron-right",
    # Page header, segmented controls (step 2)
    "plus", "settings", "calendar-plus", "chevron-left",
    # Confidentiality levels (ui/level_badge.html)
    "globe", "flask-conical", "factory", "lock",
    # Page-header actions of the detail pages
    "pencil", "copy",
)  # fmt: skip

_OPEN = (
    '<svg class="icon" aria-hidden="true" focusable="false" width="24" height="24" '
    'viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" '
    'stroke-linecap="round" stroke-linejoin="round">'
)
_FORBIDDEN = re.compile(r"style|script|\son\w+\s*=|href|<!(?!--)|<\?|foreignObject", re.I)
_SHAPES = re.compile(r"<(path|circle|rect|line|polyline|polygon|ellipse)\b[^<>]*/>")


def clean_svg(source: str) -> str:
    """The shapes of a Lucide SVG inside our own <svg> tag; ValueError otherwise."""
    body = re.sub(r"<!--.*?-->", "", source, flags=re.S)
    if _FORBIDDEN.search(body):
        raise ValueError("unexpected content in an icon")
    match = re.fullmatch(r"\s*<svg\b[^<>]*>(.*)</svg>\s*", body, flags=re.S)
    if match is None:
        raise ValueError("not a single <svg> element")
    shapes = _SHAPES.findall(match.group(1))
    if not shapes or _SHAPES.sub("", match.group(1)).strip():
        raise ValueError("an icon holds simple shapes only")
    return _OPEN + "".join(m.group(0) for m in _SHAPES.finditer(match.group(1))) + "</svg>"


@cache
def svg(name: str) -> SafeString:
    if name not in ICON_NAMES:
        raise ValueError(f"unknown icon: {name!r}")
    # Safe: the file passed clean_svg (simple shapes, no attribute we did not write
    # other than the geometry of the shapes).
    markup = clean_svg((LUCIDE / f"{name}.svg").read_text(encoding="utf-8"))
    return mark_safe(markup)  # noqa: S308  # nosec B308 B703
