"""{% icon "name" %}, {% dashboard_tiles blocks as tiles %} and |short_date."""

from __future__ import annotations

import datetime as dt
from typing import Any

from django import template
from django.utils import formats, timezone
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


@register.filter
def short_date(value: dt.date | dt.datetime | None) -> str:
    """« 18 nov. »: the day, a no-break space, the French month abbreviation
    (format "N"; "M" would give « Nov »). An instant shows its day in TIME_ZONE."""
    if value is None or value == "":
        return ""
    if isinstance(value, dt.datetime) and timezone.is_aware(value):
        value = timezone.localtime(value)
    return formats.date_format(value, "j\u00a0N")
