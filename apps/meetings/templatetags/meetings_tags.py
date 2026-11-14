"""Template tags of the meetings, used by other modules' pages."""

from __future__ import annotations

from typing import Any

from django import template

from apps.accounts.permissions import Action, can

register = template.Library()


@register.inclusion_tag("meetings/_objective_actions_slot.html", takes_context=True)
def linked_actions(context: dict[str, Any], objective: Any) -> dict[str, Any]:
    """The « Linked actions » section of an objective's page, loaded by htmx.

    Shown only to MEETING_READ holders (never to a CSI member): the check is
    here, the fragment's view checks again and the service a third time.
    """
    request = context["request"]
    thesis = context["thesis"]
    return {
        "show": can(request.user, Action.MEETING_READ, thesis),
        "thesis": thesis,
        "objective": objective,
    }
