"""Tabs of a module: its sub-sections, each one a page of its own.

A tab is shown under the very condition the old link bar showed its link (all of
them, except the travel budget, which follows travel.services.budget_payers, and
the approvals to decide, which follow the queue, pending_orders_for): the views
of the module re-check every request anyway. A tab is current on its
own page and on the pages under it (a meeting is under "Meetings").
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from django.http import HttpRequest
from django.urls import reverse
from django.utils.functional import Promise
from django.utils.translation import gettext_lazy as _
from django.utils.translation import pgettext_lazy


def _budget_visible(request: HttpRequest, thesis: Any) -> bool:
    from apps.travel import services

    return bool(services.budget_payers(request.user, thesis))


def _decides_approvals(request: HttpRequest, thesis: Any) -> bool:
    """The rule of travel.services.pending_orders_for: the right to decide, and the
    role of a payer's person (tutor: ST, director: LIRMM). Never a co-supervisor."""
    from apps.accounts.permissions import Action, can, roles_of
    from apps.travel.services import DECIDER_ROLE

    if not can(request.user, Action.VALIDATE_MISSION_ORDER, thesis):
        return False
    roles = roles_of(request.user, thesis)
    return any(role in roles for role in DECIDER_ROLE.values())


def _can(action_name: str) -> Callable[[HttpRequest, Any], bool]:
    """The tab of a page guarded by ``action_name`` (the view's own action)."""

    def check(request: HttpRequest, thesis: Any) -> bool:
        from apps.accounts.permissions import Action, can

        return can(request.user, Action(action_name), thesis)

    return check


@dataclass(frozen=True)
class Tab:
    label: str | Promise
    url_name: str  # in the module's namespace
    pages: tuple[str, ...]  # url names (same namespace) under this tab
    only_if: Callable[[HttpRequest, Any], bool] | None = None


@dataclass(frozen=True)
class Module:
    label: str | Promise
    tabs: tuple[Tab, ...]


MODULES: dict[str, Module] = {
    "meetings": Module(
        _("Meetings"),
        (
            Tab(_("Meetings"), "list", ("list", "new", "detail", "edit", "duplicate")),
            Tab(_("Decision register"), "register", ("register",)),
        ),
    ),
    "conferences": Module(
        _("Conferences"),
        (
            Tab(
                _("Deadlines"),
                "deadlines",
                ("deadlines", "policy", "policy_edit", "new_venue", "venue", "edition"),
            ),
            Tab(
                _("Submissions"),
                "submissions",
                ("submissions", "new_submission", "submission", "edit_submission"),
            ),
        ),
    ),
    "travel": Module(
        _("Travel"),
        (
            Tab(pgettext_lazy("module tab", "Calendar"), "calendar", ("calendar", "month")),
            Tab(_("Missions"), "missions", ("missions", "new_mission", "mission", "edit_mission")),
            Tab(_("ST / LIRMM time"), "split", ("split",)),
            Tab(_("Budget"), "budget", ("budget",), only_if=_budget_visible),
            Tab(pgettext_lazy("module tab", "Settings"), "settings", ("settings",)),
            # "Approval", never "order": the official mission order is ST's or the LIRMM's.
            Tab(
                pgettext_lazy("module tab", "To decide"),
                "orders",
                ("orders",),
                only_if=_decides_approvals,
            ),
        ),
    ),
    # M9a: the summary and the settings are the student's (PORTFOLIO_EXPORT,
    # TRAINING_WRITE): the tabs follow the views' own actions.
    "portfolio": Module(
        _("Trainings"),
        (
            Tab(_("Trainings"), "trainings", ("trainings", "new", "detail", "edit")),
            Tab(_("ADUM summary"), "adum", ("adum",), only_if=_can("PORTFOLIO_EXPORT")),
            Tab(
                pgettext_lazy("module tab", "Settings"),
                "settings",
                ("settings",),
                only_if=_can("TRAINING_WRITE"),
            ),
        ),
    ),
}


def tabs_for(request: HttpRequest, thesis: Any, module: str) -> dict[str, Any]:
    """The tabs of ``module`` for this viewer, the current one marked."""
    spec = MODULES[module]
    match = request.resolver_match
    here = match.url_name if match is not None and match.namespace == module else None
    tabs = [
        {
            "label": tab.label,
            "url": reverse(f"{module}:{tab.url_name}", kwargs={"thesis_id": thesis.pk}),
            "current": here in tab.pages,
        }
        for tab in spec.tabs
        if tab.only_if is None or tab.only_if(request, thesis)
    ]
    return {"module_label": spec.label, "tabs": tabs}
