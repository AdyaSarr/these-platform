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
from ..agenda import Entry, agenda_for
from ..documents import LEVEL_ICONS, LEVEL_TONES, numbered_titles
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


@register.filter
def ago(value: dt.datetime | None) -> str:
    """« il y a 5 minutes » within a week (one unit, a no-break space before it),
    the short date beyond (M13a review of the activity rows)."""
    from django.utils.timesince import timesince
    from django.utils.translation import gettext

    if value is None:
        return ""
    if timezone.now() - value >= dt.timedelta(days=7):
        return short_date(value)
    delta = timesince(value, depth=1).replace(" ", "\u00a0")
    return gettext("%(delta)s ago") % {"delta": delta}


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


@register.simple_tag(takes_context=True)
def dashboard_agenda(context: Context, blocks: list[tuple[Any, dict[str, Any]]]) -> list[Entry]:
    """The « À venir » card (apps/ui/agenda.py): filtered per viewer, then cut."""
    return agenda_for(context["request"], context["thesis"], blocks)


@register.simple_tag
def document_rows(documents: list[Any]) -> list[tuple[Any, str]]:
    """(document, label) for the list: ``documents`` are the viewer's readable ones."""
    labels = numbered_titles(documents)
    return [(d, labels[d.pk]) for d in documents]


@register.simple_tag(takes_context=True)
def document_label(context: Context, document: Any) -> str:
    """The label of ``document`` in the viewer's list (the detail page's title)."""
    from apps.vault.services import readable_documents

    request = context["request"]
    readable = readable_documents(request.user, context["thesis"])
    return numbered_titles(readable).get(document.pk, str(document.title))


@register.inclusion_tag("ui/level_badge.html")
def level_badge(value: Any) -> dict[str, Any]:
    """The badge of a confidentiality level; nothing for a value outside the list."""
    from apps.core.confidentiality import ConfidentialityLevel

    try:
        level = ConfidentialityLevel(value)
    except ValueError:
        return {"level": None}
    return {
        "level": level.value,
        "label": level.label,
        "tone": LEVEL_TONES[level.value],
        "icon": LEVEL_ICONS[level.value],
    }


@register.simple_tag
def latest_document_rows(data: dict[str, Any]) -> list[tuple[Any, str]]:
    """(document, label) of the dashboard's latest documents, numbered among all
    the readable ones, like the list (vault.services.dashboard_block)."""
    labels = numbered_titles(data.get("readable", []))
    return [(d, labels.get(d.pk, d.title)) for d in data.get("latest", [])]
