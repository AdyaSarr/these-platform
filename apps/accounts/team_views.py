"""Team mutations (POST only). Order of checks, by mixin order:
1. permissions.py (403 if the user lacks the action),
2. re-authentication with a second factor less than 10 min old (reauth.py),
3. the service, which re-checks the right and applies the rules.
"""

from __future__ import annotations

from typing import Any

from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.http import Http404, HttpRequest, HttpResponse, HttpResponseBase
from django.shortcuts import redirect, render
from django.utils.translation import gettext as _
from django.views import View

from . import services
from .forms import ChangeRoleForm, ExtendForm, InviteForm
from .models import Invitation, Membership, User
from .permissions import Action, ThesisPermissionRequiredMixin
from .reauth import RecentAuthRequiredMixin
from .roles import Role
from .services import CSI_DEFAULT_EXTENSION_DAYS, TeamError
from .views import team_context


def _signed_in_user(request: HttpRequest) -> User:
    """The permission mixin already refused anonymous users; never rely on assert (-O)."""
    user = request.user
    if not isinstance(user, User):
        raise PermissionDenied
    return user


class _TeamAction(ThesisPermissionRequiredMixin, RecentAuthRequiredMixin, View):
    http_method_names = ["post"]  # noqa: RUF012  (django-stubs: instance attribute)

    def _back(self) -> HttpResponse:
        return redirect("accounts:team", thesis_id=self.thesis.pk)

    def _user(self) -> User:
        return _signed_in_user(self.request)

    def _membership(self, membership_id: Any) -> Membership:
        m = Membership.objects.active().filter(pk=membership_id, thesis=self.thesis).first()
        if m is None:
            raise Http404
        return m

    def _invitation(self, invitation_id: Any) -> Invitation:
        inv = Invitation.objects.filter(
            pk=invitation_id, thesis=self.thesis, used_at__isnull=True, revoked_at__isnull=True
        ).first()
        if inv is None:
            raise Http404
        return inv

    def _run(self, action: Any, success: str) -> HttpResponse:
        try:
            action()
        except TeamError as exc:
            messages.error(self.request, str(exc))
        else:
            messages.success(self.request, success)
        return self._back()


class InviteView(_TeamAction):
    required_action = Action.INVITE_MEMBER

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponseBase:
        form = InviteForm(request.POST)
        if not form.is_valid():
            context = team_context(request, self.thesis, invite_form=form)
            return render(request, "accounts/team.html", context, status=400)
        return self._run(
            lambda: services.create_invitation(
                thesis=self.thesis,
                email=form.cleaned_data["email"],
                role=Role(form.cleaned_data["role"]),
                organization=form.resolved_organization(),
                by=self._user(),
                request=request,
            ),
            _("Invitation sent."),
        )


class ChangeRoleView(_TeamAction):
    required_action = Action.CHANGE_ROLE

    def post(
        self, request: HttpRequest, *args: Any, membership_id: Any, **kwargs: Any
    ) -> HttpResponseBase:
        m = self._membership(membership_id)
        form = ChangeRoleForm(request.POST)
        if not form.is_valid():
            messages.error(request, _("Choose a valid role."))
            return self._back()
        new_role = Role(form.cleaned_data["role"])
        return self._run(
            lambda: services.change_role(m, new_role, by=self._user(), request=request),
            _("Role changed."),
        )


class RemoveMemberView(_TeamAction):
    required_action = Action.REMOVE_MEMBER

    def post(
        self, request: HttpRequest, *args: Any, membership_id: Any, **kwargs: Any
    ) -> HttpResponseBase:
        m = self._membership(membership_id)
        return self._run(
            lambda: services.end_membership(m, by=self._user(), request=request),
            _("Member removed: their access ended immediately."),
        )


class ExtendMembershipView(_TeamAction):
    required_action = Action.EXTEND_MEMBERSHIP

    def post(
        self, request: HttpRequest, *args: Any, membership_id: Any, **kwargs: Any
    ) -> HttpResponseBase:
        m = self._membership(membership_id)
        form = ExtendForm(request.POST)
        if not form.is_valid():
            messages.error(request, _("The extension must be between 1 and 90 days."))
            return self._back()
        days = form.cleaned_data["days"] or CSI_DEFAULT_EXTENSION_DAYS
        return self._run(
            lambda: services.extend_membership(m, by=self._user(), days=days, request=request),
            _("CSI access extended."),
        )


class ResendInvitationView(_TeamAction):
    """A new invitation (new token) replacing an open one, e.g. after a failed e-mail."""

    required_action = Action.INVITE_MEMBER

    def post(
        self, request: HttpRequest, *args: Any, invitation_id: Any, **kwargs: Any
    ) -> HttpResponseBase:
        inv = self._invitation(invitation_id)
        return self._run(
            lambda: services.create_invitation(
                thesis=self.thesis,
                email=inv.email,
                role=Role(inv.role),
                organization=inv.organization,
                by=self._user(),
                request=request,
            ),
            _("Invitation sent again."),
        )


class RevokeInvitationView(ThesisPermissionRequiredMixin, View):
    """Revoking only removes a possible access: no re-authentication (plan, step 4)."""

    required_action = Action.INVITE_MEMBER
    http_method_names = ["post"]  # noqa: RUF012  (django-stubs: instance attribute)

    def post(
        self, request: HttpRequest, *args: Any, invitation_id: Any, **kwargs: Any
    ) -> HttpResponseBase:
        inv = Invitation.objects.filter(pk=invitation_id, thesis=self.thesis).first()
        if inv is None:
            raise Http404
        try:
            services.revoke_invitation(inv, by=_signed_in_user(request), request=request)
        except TeamError as exc:
            messages.error(request, str(exc))
        else:
            messages.success(request, _("Invitation revoked."))
        return redirect("accounts:team", thesis_id=self.thesis.pk)
