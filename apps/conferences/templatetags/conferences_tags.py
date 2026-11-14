"""Template tags of the conferences, used by other modules' pages."""

from __future__ import annotations

from typing import Any

from django import template

from apps.accounts.permissions import Action, can

register = template.Library()


@register.inclusion_tag("conferences/_objective_submissions_slot.html", takes_context=True)
def linked_submissions(context: dict[str, Any], objective: Any) -> dict[str, Any]:
    """« Linked submissions » on an objective's page (SUBMISSION_READ holders only)."""
    request = context["request"]
    thesis = context["thesis"]
    return {
        "show": can(request.user, Action.SUBMISSION_READ, thesis),
        "thesis": thesis,
        "objective": objective,
    }
