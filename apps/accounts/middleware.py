"""Mandatory second factor: an authenticated user without one only reaches enrollment."""

from __future__ import annotations

from collections.abc import Callable

from allauth.mfa.utils import is_mfa_enabled
from django.conf import settings
from django.http import HttpRequest, HttpResponse, HttpResponseRedirect
from django.urls import reverse

# Paths a signed-in user without a second factor may still reach.
_ALLOWED_PREFIXES = (
    "/accounts/2fa/",  # enrollment (TOTP, WebAuthn, recovery codes)
    "/accounts/logout/",
    "/accounts/reauthenticate/",  # allauth may ask for the password before enrolling
    "/health/",
)


class MfaRequiredMiddleware:
    """Every authenticated request needs an enrolled TOTP or WebAuthn factor.

    allauth already asks the factor at sign-in for users who have one; this
    covers the only remaining case, a fresh account (invitation accepted) that
    has not enrolled yet. Recovery codes alone do not count.
    """

    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response
        self.allowed = ("/" + settings.STATIC_URL.lstrip("/"), *_ALLOWED_PREFIXES)

    def __call__(self, request: HttpRequest) -> HttpResponse:
        user = request.user
        if (
            user.is_authenticated
            and not request.path.startswith(self.allowed)
            and not is_mfa_enabled(user, types=["totp", "webauthn"])
        ):
            target = reverse("mfa_index")
            if request.headers.get("HX-Request"):
                # htmx would swap the redirected page into a fragment: ask for
                # a full navigation instead.
                return HttpResponse(status=200, headers={"HX-Redirect": target})
            return HttpResponseRedirect(target)
        return self.get_response(request)
