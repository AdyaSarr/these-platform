"""Views of the iCal feed (M12).

``feed``: the subscription itself. It never touches the user nor the session (no
cookie read or set), answers every refusal with the same 404 after one query,
and counts its rate limits before the lookup. Its responses are marked as
journaled: Django writes no « Not Found: /ical/… » line.

Create and regenerate need a recent second factor (rule 6); revoke does not.
"""

from __future__ import annotations

from typing import Any

from django.conf import settings
from django.core.exceptions import PermissionDenied
from django.http import HttpRequest, HttpResponse, HttpResponseBase
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils.cache import patch_cache_control
from django.views import View
from django.views.decorators.http import require_safe

from apps.accounts.permissions import Action, ThesisPermissionRequiredMixin
from apps.accounts.reauth import RecentAuthRequiredMixin
from apps.vault.crypto import hash_token

from . import calendar, services, throttle

NOT_FOUND = b"Not found.\n"
TOO_MANY = b"Too many requests.\n"


def _plain(body: bytes, status: int) -> HttpResponse:
    response = HttpResponse(body, status=status, content_type="text/plain; charset=utf-8")
    return _private(response)


def _private(response: HttpResponse) -> HttpResponse:
    response["Cache-Control"] = "private, no-store"
    response["X-Robots-Tag"] = "noindex, nofollow"
    response["Referrer-Policy"] = "no-referrer"
    response._has_been_logged = True  # type: ignore[attr-defined]  # no « Not Found: path »
    return response


@require_safe
def feed(request: HttpRequest, token: str) -> HttpResponse:
    if throttle.over_limit(hash_token(token), request):
        return _plain(TOO_MANY, 429)
    found = services.find(token)
    if found is None:
        return _plain(NOT_FOUND, 404)
    services.touch(found)
    response = HttpResponse(calendar.build(found), content_type="text/calendar; charset=utf-8")
    return _private(response)


class _Guard(ThesisPermissionRequiredMixin):
    """A member of the thesis (VIEW_THESIS), with a role that may have a feed."""

    required_action = Action.VIEW_THESIS


def _back(thesis: Any) -> str:
    return reverse("activity:preferences", kwargs={"thesis_id": thesis.pk})


def _shown_once(request: HttpRequest, thesis: Any, token: str) -> HttpResponse:
    url = settings.SITE_URL + reverse("feeds:ical", kwargs={"token": token})
    webcal = "webcal://" + url.split("://", 1)[1]
    response = render(
        request,
        "feeds/created.html",
        {"thesis": thesis, "url": url, "webcal": webcal, "back": _back(thesis)},
    )
    patch_cache_control(response, private=True, no_store=True)
    return response


class CreateView(_Guard, RecentAuthRequiredMixin, View):
    http_method_names = ["post"]  # noqa: RUF012 (Django declares it as a list)

    def post(self, request: HttpRequest, thesis_id: Any) -> HttpResponse:
        if not services.allowed(request.user, self.thesis):
            raise PermissionDenied
        _feed, token = services.create(user=request.user, thesis=self.thesis, recent_mfa=True)
        return _shown_once(request, self.thesis, token)


class RegenerateView(_Guard, RecentAuthRequiredMixin, View):
    http_method_names = ["post"]  # noqa: RUF012 (Django declares it as a list)

    def post(self, request: HttpRequest, thesis_id: Any) -> HttpResponse:
        if not services.allowed(request.user, self.thesis):
            raise PermissionDenied
        _feed, token = services.regenerate(user=request.user, thesis=self.thesis, recent_mfa=True)
        return _shown_once(request, self.thesis, token)


class RevokeView(_Guard, View):
    """No second factor: a reduction of access, useful in an emergency."""

    http_method_names = ["post"]  # noqa: RUF012 (Django declares it as a list)

    def post(self, request: HttpRequest, thesis_id: Any) -> HttpResponseBase:
        services.revoke(user=request.user, thesis=self.thesis)
        return redirect(_back(self.thesis))
