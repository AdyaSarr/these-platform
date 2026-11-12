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

from django.contrib.auth import login
from django.contrib.auth.views import redirect_to_login
from django.http import HttpRequest, HttpResponse
from django.shortcuts import render
from django.urls import reverse
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET, require_POST

from . import services
from .forms import NewAccountForm
from .models import Invitation, User
from .services import InvalidInvitationError, TeamError

SESSION_KEY = "invitation_id"


def _invalid(request: HttpRequest) -> HttpResponse:
    """One answer for every invalid case: unknown, expired, used, revoked, wrong account."""
    return render(request, "accounts/invitation_invalid.html", status=400)


@never_cache
@require_GET
def invitation_link(request: HttpRequest, token: str) -> HttpResponse:
    try:
        services.check_token(token)
    except InvalidInvitationError:
        return _invalid(request)
    response = render(request, "accounts/invitation_link.html", {"token": token})
    # The « Continuer » POST would otherwise carry this URL, token included, in
    # its Referer header (same-origin policy), where proxies log it.
    response["Referrer-Policy"] = "no-referrer"
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
        if User.objects.filter(email=inv.email).exists():
            if not request.user.is_authenticated:
                return render(request, "accounts/invitation_signin.html")
            if request.user.email != inv.email:
                return _invalid(request)
            return render(request, "accounts/invitation_confirm.html")
        if request.user.is_authenticated:
            return _invalid(request)
        return self._form(request, NewAccountForm(email=inv.email))

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        inv = self._invitation(request)
        if inv is None:
            return _invalid(request)
        if User.objects.filter(email=inv.email).exists():
            return self._accept_existing(request, inv)
        if request.user.is_authenticated:  # signed in as someone else
            return _invalid(request)
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
        # login() rotates the session key (no session fixation).
        login(request, user, backend="django.contrib.auth.backends.ModelBackend")
        return self._done(request)

    def _accept_existing(self, request: HttpRequest, inv: Invitation) -> HttpResponse:
        if not request.user.is_authenticated:
            return redirect_to_login(reverse("invitation_accept"))
        if request.user.email != inv.email:
            return _invalid(request)
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
