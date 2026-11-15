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
