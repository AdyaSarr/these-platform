"""Template variables of base.html (a context processor adds them to every page).

Lazy: an htmx fragment, which never extends base.html, makes no query for them.
"""

from __future__ import annotations

from typing import Any

from django.http import HttpRequest
from django.utils.functional import SimpleLazyObject

from .navigation import nav_for, thesis_for_header
from .theme import THEMES, back_to, theme_of


def ui(request: HttpRequest) -> dict[str, Any]:
    return {
        "ui_theme": theme_of(request),
        # A refused form (apps.core.views.csrf_failure) copies nothing of the request.
        "ui_next": "/" if getattr(request, "ui_echo_nothing", False) else back_to(request),
        "ui_themes": THEMES,
        "ui_nav": SimpleLazyObject(lambda: nav_for(request)),
        "ui_thesis": SimpleLazyObject(lambda: thesis_for_header(request)),
        # The bell (M11a): on a page of a thesis the guard authorised, to a reader.
        "ui_bell": SimpleLazyObject(lambda: _bell(request)),
    }


def _bell(request: HttpRequest) -> Any:
    thesis = thesis_for_header(request)
    if thesis is None:
        return None
    from apps.activity.services import bell, grouped

    state = bell(request.user, thesis)
    return {"thesis": thesis, "state": state, "groups": grouped(state.latest)}
