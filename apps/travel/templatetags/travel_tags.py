"""Template tags of the travel module."""

from __future__ import annotations

from typing import Any

from django import template

from apps.travel import services

register = template.Library()


@register.simple_tag(takes_context=True)
def budget_visible(context: Any, thesis: Any) -> bool:
    """Whether the budget page is for this user (never a co-supervisor)."""
    request = context.get("request")
    return bool(request is not None and services.budget_payers(request.user, thesis))


@register.inclusion_tag("travel/_links.html", takes_context=True)
def travel_links(context: Any, obj: Any) -> dict[str, Any]:
    """The "Travel" section of a meeting (M5) or a submission (M6): the linked missions
    the viewer sees (visible_to: a co-supervisor, LIRMM only); "plan a mission" for
    the student. Nothing for a viewer without TRAVEL_READ."""
    from django.urls import reverse

    from apps.accounts.permissions import Action, can
    from apps.accounts.statuses import is_open_for
    from apps.conferences.models import Submission
    from apps.meetings.models import Meeting
    from apps.travel.models import Mission

    request = context.get("request")
    thesis = obj.thesis
    user = getattr(request, "user", None)
    if user is None or not can(user, Action.TRAVEL_READ, thesis):
        return {"thesis": thesis, "missions": [], "plan_url": "", "hidden": True}
    field = "meeting" if isinstance(obj, Meeting) else "submission"
    if not isinstance(obj, Meeting | Submission):
        raise TypeError("travel_links takes a meeting or a submission")
    missions = list(
        Mission.objects.visible_to(user, thesis).filter(**{field: obj}).order_by("start_date")
    )
    plan_url = ""
    # M13a: no new mission after the defence (apps/accounts/statuses.py).
    if can(user, Action.TRAVEL_WRITE, thesis) and is_open_for(thesis, "travel"):
        plan_url = (
            reverse("travel:new_mission", kwargs={"thesis_id": thesis.pk}) + f"?{field}={obj.pk}"
        )
    return {"thesis": thesis, "missions": missions, "plan_url": plan_url}
