"""The sidebar: one entry per module page, shown only to users holding the action
that page requires (a test checks each action against the view's own guard).

Built from request.thesis, which apps.accounts.permissions sets only after the
guard authorised the page: a refused page, or a page outside a thesis, has no
sidebar (and so names no thesis).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from django.http import HttpRequest
from django.urls import reverse
from django.utils.functional import Promise
from django.utils.translation import gettext_lazy as _

from apps.accounts.permissions import Action, allowed_actions


@dataclass(frozen=True)
class NavItem:
    key: str
    label: str | Promise
    icon: str
    url_name: str
    action: Action
    # The pages where this entry is the current one: (namespace, url_name prefix).
    current: tuple[str, str]


NAV: tuple[NavItem, ...] = (
    NavItem("home", _("Home"), "house", "accounts:dashboard", Action.VIEW_THESIS,
            ("accounts", "dashboard")),
    NavItem("admin_file", _("Administrative file"), "folder-open", "admin_file:file",
            Action.CIFRE_FILE_READ, ("admin_file", "")),
    NavItem("objectives", _("Objectives"), "target", "objectives:dashboard",
            Action.OBJECTIVE_READ, ("objectives", "")),
    NavItem("meetings", _("Meetings"), "calendar-days", "meetings:list", Action.MEETING_READ,
            ("meetings", "")),
    NavItem("conferences", _("Conferences"), "presentation", "conferences:deadlines",
            Action.CONFERENCE_READ, ("conferences", "")),
    NavItem("travel", _("Travel"), "plane", "travel:calendar", Action.TRAVEL_READ,
            ("travel", "")),
    NavItem("documents", _("Documents"), "file-lock", "vault:documents", Action.DOCUMENT_READ,
            ("vault", "")),
    NavItem("team", _("Team"), "users", "accounts:team", Action.VIEW_TEAM, ("accounts", "team")),
    NavItem("csi", _("CSI file"), "clipboard-list", "accounts:csi_file", Action.CSI_FILE_READ,
            ("accounts", "csi")),
)  # fmt: skip


def authorised_thesis(request: HttpRequest) -> Any:
    """The thesis the guard authorised for this request, or None."""
    return getattr(request, "thesis", None)


def allowed(request: HttpRequest) -> frozenset[Action]:
    """The actions of the user on the authorised thesis (once per request)."""
    cached: frozenset[Action] | None = getattr(request, "_ui_allowed", None)
    if cached is None:
        thesis = authorised_thesis(request)
        cached = frozenset() if thesis is None else allowed_actions(request.user, thesis)
        request._ui_allowed = cached  # type: ignore[attr-defined]
    return cached


def _is_current(request: HttpRequest, item: NavItem) -> bool:
    match = getattr(request, "resolver_match", None)
    if match is None:
        return False
    namespace, prefix = item.current
    return match.namespace == namespace and (match.url_name or "").startswith(prefix)


def nav_for(request: HttpRequest) -> list[dict[str, Any]]:
    thesis = authorised_thesis(request)
    if thesis is None:
        return []
    actions = allowed(request)
    return [
        {
            "key": item.key,
            "label": item.label,
            "icon": item.icon,
            "url": reverse(item.url_name, kwargs={"thesis_id": thesis.pk}),
            "current": _is_current(request, item),
        }
        for item in NAV
        if item.action in actions
    ]


def thesis_for_header(request: HttpRequest) -> Any:
    """The thesis named in the header: only to users who may view it."""
    thesis = authorised_thesis(request)
    if thesis is None or Action.VIEW_THESIS not in allowed(request):
        return None
    return thesis
