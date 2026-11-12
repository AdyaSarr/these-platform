"""Recent re-authentication before sensitive actions (CLAUDE.md rule 6).

Other modules depend on THIS API, not on allauth:

    @recent_auth_required                 # second factor within 10 min (default)
    @recent_auth_required(mfa=False)      # password is enough: write it explicitly
    class MyView(ThesisPermissionRequiredMixin, RecentAuthRequiredMixin, View): ...

Put RecentAuthRequiredMixin AFTER ThesisPermissionRequiredMixin: a user without
the right gets 403 straight away instead of being asked to re-authenticate.

"Recent" = an authentication of the required kind recorded in this session less
than REAUTH_MAX_AGE seconds ago, whether at sign-in or at an explicit
re-authentication. allauth itself only counts explicit re-authentications; a
fresh sign-in with the second factor is at least as strong, so we count it too.

This module is the only one reading allauth's per-session authentication
records (an internal API); the end-to-end tests in test_reauth.py and
test_mfa.py go through allauth's real flows and catch any change on upgrade.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from functools import wraps
from typing import Any, overload
from urllib.parse import urlencode, urlsplit

from django.contrib.auth.views import redirect_to_login
from django.http import HttpRequest, HttpResponse, HttpResponseBase, HttpResponseRedirect
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme

REAUTH_MAX_AGE = 600  # seconds (= ACCOUNT_REAUTHENTICATION_TIMEOUT)
_MFA_METHODS = frozenset({"mfa"})
_ANY_METHODS = frozenset({"mfa", "password"})


def _records(request: HttpRequest) -> list[dict[str, Any]]:
    from allauth.account.internal.flows.login import get_authentication_records

    records = get_authentication_records(request, request.user)
    return [r for r in records if isinstance(r, dict)]


def authenticated_recently(
    request: HttpRequest, *, mfa: bool = True, max_age: int = REAUTH_MAX_AGE
) -> bool:
    if not request.user.is_authenticated:
        return False
    accepted = _MFA_METHODS if mfa else _ANY_METHODS
    now = time.time()
    for record in _records(request):
        at = record.get("at")
        if (
            record.get("method") in accepted
            and isinstance(at, int | float)
            and 0 <= now - at < max_age
        ):
            return True
    return False


def record_password_authentication(request: HttpRequest, user: Any) -> None:
    """Record that ``user`` has just proven their password in this session.

    Used after account creation (invitation accepted): the password was typed
    seconds ago, so enrolling the second factor must not ask for it again.
    """
    from allauth.account.internal.flows.login import record_authentication

    record_authentication(request, user, method="password", reauthenticated=True)


def _return_path(request: HttpRequest) -> str:
    """Where to come back after re-authenticating: this page, or a same-site referrer."""
    if request.method == "GET":
        return request.get_full_path()
    candidate = request.headers.get("HX-Current-URL") or request.headers.get("Referer") or ""
    if candidate and url_has_allowed_host_and_scheme(
        candidate, allowed_hosts={request.get_host()}, require_https=request.is_secure()
    ):
        parts = urlsplit(candidate)
        return parts.path + (f"?{parts.query}" if parts.query else "")
    return "/"


def reauth_response(request: HttpRequest, *, mfa: bool) -> HttpResponse:
    page = reverse("mfa_reauthenticate" if mfa else "account_reauthenticate")
    url = f"{page}?{urlencode({'next': _return_path(request)})}"
    if request.headers.get("HX-Request"):
        return HttpResponse(status=200, headers={"HX-Redirect": url})
    return HttpResponseRedirect(url)


def _check(request: HttpRequest, mfa: bool) -> HttpResponseBase | None:
    if not request.user.is_authenticated:
        return redirect_to_login(request.get_full_path())
    if not authenticated_recently(request, mfa=mfa):
        return reauth_response(request, mfa=mfa)
    return None


_View = Callable[..., HttpResponseBase]


@overload
def recent_auth_required(view: _View, *, mfa: bool = True) -> _View: ...
@overload
def recent_auth_required(view: None = None, *, mfa: bool = True) -> Callable[[_View], _View]: ...


def recent_auth_required(
    view: _View | None = None, *, mfa: bool = True
) -> _View | Callable[[_View], _View]:
    """Decorator for function views. Bare form = second factor required."""

    def decorator(fn: _View) -> _View:
        @wraps(fn)
        def wrapped(request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponseBase:
            return _check(request, mfa) or fn(request, *args, **kwargs)

        wrapped.recent_auth_mfa = mfa  # type: ignore[attr-defined]
        return wrapped

    return decorator(view) if view is not None else decorator


class RecentAuthRequiredMixin:
    """Class-based equivalent. Set ``reauth_mfa = False`` to accept the password alone."""

    reauth_mfa: bool = True

    def dispatch(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponseBase:
        refused = _check(request, self.reauth_mfa)
        if refused is not None:
            return refused
        response: HttpResponseBase = super().dispatch(request, *args, **kwargs)  # type: ignore[misc]
        return response
