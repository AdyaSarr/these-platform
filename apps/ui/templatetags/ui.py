"""{% icon "name" %}, {% dashboard_tiles blocks as tiles %}, |short_date, the page
header ({% page_header %}) and the tabs of a module ({% module_tabs "travel" %})."""

from __future__ import annotations

import datetime as dt
from typing import Any

from django import template
from django.template.base import NodeList, Parser, Token, token_kwargs
from django.template.context import Context
from django.utils import formats, timezone
from django.utils.safestring import SafeString

from .. import icons
from ..tabs import tabs_for
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


# --------------------------------------------------------------------------- page header

# {% page_header [parent_url=... parent_label=...] %}title
#   [{% subtitle %}subtitle] [{% actions %}buttons (ui/action.html)]
# {% endpage_header %}
# Sections, not arguments: a title may be a sealed text rendered by an include.
_SECTIONS = ("subtitle", "actions")


class PageHeaderNode(template.Node):
    def __init__(self, parts: dict[str, NodeList], kwargs: dict[str, Any]) -> None:
        self.parts = parts
        self.kwargs = kwargs

    def render(self, context: Context) -> str:
        # NodeList.render() returns a SafeString: already escaped by the template.
        values: dict[str, Any] = {}
        for name, nodelist in self.parts.items():
            rendered = nodelist.render(context)
            values[name] = rendered if rendered.strip() else None
        values.update({key: value.resolve(context) for key, value in self.kwargs.items()})
        page = context.template.engine.get_template("ui/page_header.html")  # type: ignore[union-attr]
        with context.push(**values):
            return page.render(context)


@register.tag("page_header")
def page_header(parser: Parser, token: Token) -> PageHeaderNode:
    bits = token.split_contents()[1:]
    kwargs = token_kwargs(bits, parser)
    if bits or set(kwargs) - {"parent_url", "parent_label"}:
        raise template.TemplateSyntaxError("page_header takes parent_url= and parent_label= only")
    parts: dict[str, NodeList] = {}
    section, allowed = "title", list(_SECTIONS)
    while True:
        parts[section] = parser.parse((*allowed, "endpage_header"))
        tag = parser.next_token().contents
        if tag == "endpage_header":
            return PageHeaderNode(parts, kwargs)
        section = tag
        allowed = allowed[allowed.index(tag) + 1 :]  # subtitle before actions, once each


@register.inclusion_tag("ui/tabs.html", takes_context=True)
def module_tabs(context: Context, module: str) -> dict[str, Any]:
    return tabs_for(context["request"], context["thesis"], module)
