"""Theme toggle and style guide. Neither reads nor shows any thesis data."""

from __future__ import annotations

from typing import Any

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.http import Http404, HttpRequest, HttpResponse, HttpResponseBadRequest
from django.shortcuts import redirect, render
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_GET, require_POST

from .icons import ICON_NAMES
from .theme import COOKIE_NAME, MAX_AGE, THEMES


@require_POST
def set_theme(request: HttpRequest) -> HttpResponse:
    """Store the choice in a cookie, then back to the page (its path only)."""
    choice = request.POST.get("theme", "")
    if choice not in THEMES:
        return HttpResponseBadRequest()
    target = request.POST.get("next", "")
    if not (
        target.startswith("/")
        and url_has_allowed_host_and_scheme(
            target, allowed_hosts={request.get_host()}, require_https=request.is_secure()
        )
    ):
        target = "/"
    response = redirect(target)
    # "auto" is stored too: without a cookie the theme is the default (dark).
    response.set_cookie(
        COOKIE_NAME,
        choice,
        max_age=MAX_AGE,
        path="/",
        secure=settings.SESSION_COOKIE_SECURE,
        httponly=True,
        samesite="Lax",
    )
    return response


@require_GET
@login_required
def styleguide(request: HttpRequest) -> HttpResponse:
    """Every component with fictitious data; exists in DEBUG only."""
    if not settings.DEBUG:
        raise Http404
    context: dict[str, Any] = {"icon_names": ICON_NAMES}
    return render(request, "ui/styleguide.html", context)
