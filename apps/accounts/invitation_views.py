"""Invitation acceptance: the only way into the platform (no public sign-up).

Flow, shaped by the SameSite=Strict session cookie (a link clicked in a mail
client is a cross-site navigation: the browser sends no cookie, not even on a
redirect that follows it):

1. GET  /invitation/<token>/  checks the token and shows a « Continuer » button.
   It consumes and stores nothing, so mail-client link scanners have no effect.
2. POST /invitation/          (same-site, so cookies are sent) re-checks the
   token, keeps the invitation id in the session, and redirects (303) to
3. /invitation/accept/        where the invitee creates their account, or signs
   in to the existing one, and accepts. The token is no longer in any URL.
"""

from __future__ import annotations

from typing import Any

from allauth.account.adapter import get_adapter
from django.contrib.auth import login
from django.contrib.auth.views import redirect_to_login
from django.http import HttpRequest, HttpResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET, require_POST

from . import services
from .forms import NewAccountForm
from .managers import UserManager
from .models import Invitation, User
from .reauth import record_password_authentication
from .services import InvalidInvitationError, TeamError

SESSION_KEY = "invitation_id"


def _invalid(request: HttpRequest) -> HttpResponse:
    """One answer for every invalid link: unknown, expired, used, revoked."""
    return render(request, "accounts/invitation_invalid.html", status=400)


def _same_address(a: str, b: str) -> bool:
    """Compared as account creation stores them (case, surrounding spaces)."""
    return UserManager.normalize_email(a) == UserManager.normalize_email(b)


def _signed_in_as_someone_else(request: HttpRequest, inv: Invitation) -> bool:
    user = request.user
    return isinstance(user, User) and not _same_address(user.email, inv.email)


def _other_account(request: HttpRequest) -> HttpResponse:
    """A VALID invitation, but another account is signed in: nothing granted, nothing
    consumed, the invited address never shown (same page whether it has an account)."""
    request.session.pop(SESSION_KEY, None)
    response = render(request, "accounts/invitation_other_account.html", status=409)
    response["Referrer-Policy"] = "strict-origin"  # the link page's URL holds the token
    return response


@never_cache
@require_GET
def invitation_link(request: HttpRequest, token: str) -> HttpResponse:
    try:
        inv = services.check_token(token)  # validity first: an invalid link stays generic
    except InvalidInvitationError:
        return _invalid(request)
    if _signed_in_as_someone_else(request, inv):
        return _other_account(request)
    response = render(request, "accounts/invitation_link.html", {"token": token})
    # The « Continuer » POST would otherwise carry this URL, token included, in
    # its Referer header (same-origin policy), where proxies log it.
    # strict-origin: the Referer holds the origin only, never the path. NOT
    # no-referrer: under it the browser sends "Origin: null" with the POST (Fetch
    # standard), which Django's CSRF check refuses; "null" is never trusted.
    response["Referrer-Policy"] = "strict-origin"
    return response


@never_cache
@require_POST
def invitation_continue(request: HttpRequest) -> HttpResponse:
    try:
        inv = services.check_token(request.POST.get("token", ""))
    except InvalidInvitationError:
        return _invalid(request)
    request.session[SESSION_KEY] = str(inv.pk)
    # 303: the browser follows with a GET, and a reload does not resubmit the token.
    return HttpResponse(status=303, headers={"Location": reverse("invitation_accept")})


def _account_of(inv: Invitation) -> User | None:
    return User.objects.filter(email=UserManager.normalize_email(inv.email)).first()


@never_cache
@require_POST
def invitation_logout(request: HttpRequest) -> HttpResponse:
    """« Se déconnecter » of the « another address » page: allauth's logout (POST,
    CSRF-checked), then ALWAYS the home page: no redirect read from the request."""
    get_adapter(request).logout(request)
    return redirect(reverse("core:home"))


@method_decorator(never_cache, name="dispatch")
class AcceptInvitationView(View):
    def _invitation(self, request: HttpRequest) -> Invitation | None:
        pk = request.session.get(SESSION_KEY)
        if not pk:
            return None
        try:
            return services.ensure_usable(Invitation.objects.filter(pk=pk).first())
        except (InvalidInvitationError, ValueError):
            return None

    def _done(self, request: HttpRequest) -> HttpResponse:
        request.session.pop(SESSION_KEY, None)
        return HttpResponse(status=302, headers={"Location": reverse("core:home")})

    def get(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        inv = self._invitation(request)
        if inv is None:
            return _invalid(request)
        if _signed_in_as_someone_else(request, inv):
            return _other_account(request)
        if _account_of(inv) is not None:
            if not request.user.is_authenticated:
                return render(request, "accounts/invitation_signin.html")
            return render(request, "accounts/invitation_confirm.html")
        return self._form(request, NewAccountForm(email=inv.email))

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        inv = self._invitation(request)
        if inv is None:
            return _invalid(request)
        if _signed_in_as_someone_else(request, inv):
            return _other_account(request)
        if _account_of(inv) is not None:
            return self._accept_existing(request, inv)
        form = NewAccountForm(request.POST, email=inv.email)
        if not form.is_valid():
            return self._form(request, form)
        try:
            user = services.accept_invitation_new_user(
                inv.pk,
                first_name=form.cleaned_data["first_name"],
                last_name=form.cleaned_data["last_name"],
                password=form.cleaned_data["password1"],
                request=request,
            )
        except InvalidInvitationError:
            return _invalid(request)
        except TeamError as exc:
            return self._error(request, exc)
        # login() rotates the session key (no session fixation). The password was
        # just typed: record it, so that enrolling the second factor (mandatory,
        # next page) does not ask for it again.
        login(request, user, backend="allauth.account.auth_backends.AuthenticationBackend")
        record_password_authentication(request, user)
        request.session.pop(SESSION_KEY, None)
        return HttpResponse(status=302, headers={"Location": reverse("mfa_activate_totp")})

    def _accept_existing(self, request: HttpRequest, inv: Invitation) -> HttpResponse:
        if not request.user.is_authenticated:
            return redirect_to_login(reverse("invitation_accept"))
        if _signed_in_as_someone_else(request, inv):
            return _other_account(request)
        try:
            services.accept_invitation(inv.pk, user=request.user, request=request)
        except InvalidInvitationError:
            return _invalid(request)
        except TeamError as exc:
            return self._error(request, exc)
        return self._done(request)

    def _form(self, request: HttpRequest, form: NewAccountForm) -> HttpResponse:
        return render(
            request, "accounts/invitation_accept.html", {"form": form, "email": form.email}
        )

    def _error(self, request: HttpRequest, exc: TeamError) -> HttpResponse:
        return render(request, "accounts/invitation_error.html", {"message": str(exc)}, status=409)
