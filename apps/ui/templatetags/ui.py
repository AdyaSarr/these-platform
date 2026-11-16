"""{% icon "name" %} and {% dashboard_tiles blocks as tiles %}."""

from __future__ import annotations

from typing import Any

from django import template
from django.utils.safestring import SafeString

from .. import icons
from ..tiles import Tiles, dashboard_tiles

register = template.Library()


@register.simple_tag
def icon(name: str) -> SafeString:
    return icons.svg(name)


@register.simple_tag(name="dashboard_tiles")
def dashboard_tiles_tag(blocks: list[tuple[Any, dict[str, Any]]]) -> Tiles:
    return dashboard_tiles(blocks)


# The icon of each dashboard block (accounts.views.DASHBOARD_BLOCKS keys).
BLOCK_ICONS = {
    "team": "users",
    "objectives": "target",
    "meetings": "calendar-days",
    "conferences": "presentation",
    "travel": "plane",
    "documents": "file-lock",
    "admin_file": "folder-open",
}


@register.simple_tag
def block_icon(key: str) -> SafeString:
    return icons.svg(BLOCK_ICONS.get(key, "circle-dot"))
