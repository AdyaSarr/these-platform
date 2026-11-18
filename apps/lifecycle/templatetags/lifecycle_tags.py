"""The banner of a defended or closed thesis (M13a), on each of its pages."""

from __future__ import annotations

from typing import Any

from django import template
from django.utils.translation import gettext as _

register = template.Library()


@register.inclusion_tag("lifecycle/_banner.html", takes_context=True)
def lifecycle_banner(context: dict[str, Any], thesis: Any) -> dict[str, Any]:
    """What stays open after the defence, among the modules the viewer reads only
    (a CSI member is told of none of them)."""
    from apps.accounts.models import Thesis
    from apps.accounts.permissions import Action, allowed_actions
    from apps.accounts.statuses import is_open_for

    parts: list[str] = []
    if thesis.status == Thesis.Status.DEFENDED:
        request = context.get("request")
        actions = allowed_actions(request.user, thesis) if request is not None else frozenset()
        for action, module, label in (
            (Action.DOCUMENT_READ, "documents", _("the documents")),
            (Action.CONFERENCE_READ, "conferences", _("the conferences and ST approvals")),
            (Action.TRAVEL_READ, "travel_receipts",
             _("the receipts of the missions begun before the defence")),
        ):  # fmt: skip
            if action in actions and is_open_for(thesis, module):
                parts.append(label)
    return {"thesis": thesis, "parts": ", ".join(parts)}
