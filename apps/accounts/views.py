"""Thesis pages. Thin: access goes through permissions.py, rules through services.py."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from django.db.models import QuerySet
from django.http import HttpRequest, HttpResponse
from django.shortcuts import redirect
from django.utils.functional import Promise
from django.utils.translation import gettext_lazy as _
from django.views.generic import TemplateView

from .models import Invitation, Membership
from .permissions import Action, ThesisPermissionRequiredMixin, allowed_actions, roles_of
from .roles import Role


@dataclass(frozen=True)
class DashboardBlock:
    """One dashboard section, shown only to users holding ``action`` on the thesis."""

    key: str
    action: Action
    title: str | Promise
    template: str


# Later modules replace the "upcoming" placeholder by their own block template.
DASHBOARD_BLOCKS: tuple[DashboardBlock, ...] = (
    DashboardBlock("team", Action.VIEW_TEAM, _("Team"), "accounts/blocks/team.html"),
    DashboardBlock(
        "objectives", Action.OBJECTIVE_READ, _("Objectives"), "accounts/blocks/upcoming.html"
    ),
    DashboardBlock("meetings", Action.MEETING_READ, _("Meetings"), "accounts/blocks/upcoming.html"),
    DashboardBlock(
        "documents", Action.DOCUMENT_READ, _("Documents"), "accounts/blocks/upcoming.html"
    ),
    DashboardBlock(
        "admin_file",
        Action.ADMIN_FILE_READ,
        _("Administrative file"),
        "accounts/blocks/upcoming.html",
    ),
)


class ThesisDashboardView(ThesisPermissionRequiredMixin, TemplateView):
    required_action = Action.VIEW_THESIS
    template_name = "accounts/dashboard.html"

    def get(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        # A CSI member only works on the CSI file: send them straight there.
        if Role.CSI_MEMBER in roles_of(request.user, self.thesis):
            return redirect("accounts:csi_file", thesis_id=self.thesis.pk)
        return super().get(request, *args, **kwargs)

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        allowed = allowed_actions(self.request.user, self.thesis)
        context["thesis"] = self.thesis
        context["blocks"] = [b for b in DASHBOARD_BLOCKS if b.action in allowed]
        return context


def active_team(thesis_id: Any) -> QuerySet[Membership]:
    return (
        Membership.objects.active()
        .filter(thesis_id=thesis_id)
        .select_related("user", "organization")
        .order_by("role", "user__last_name", "user__email")
    )


def team_context(request: HttpRequest, thesis: Any, **forms: Any) -> dict[str, Any]:
    """Context of the team page; the management parts only for the right holders."""
    from .forms import ChangeRoleForm, ExtendForm, InviteForm

    allowed = allowed_actions(request.user, thesis)
    context: dict[str, Any] = {
        "thesis": thesis,
        "memberships": active_team(thesis.pk),
        "can_invite": Action.INVITE_MEMBER in allowed,
        "can_change_role": Action.CHANGE_ROLE in allowed,
        "can_remove": Action.REMOVE_MEMBER in allowed,
        "can_extend": Action.EXTEND_MEMBERSHIP in allowed,
    }
    if context["can_invite"]:
        # Open invitations (with their e-mail delivery state): inviters only.
        context["invitations"] = Invitation.objects.filter(
            thesis_id=thesis.pk, used_at__isnull=True, revoked_at__isnull=True
        ).order_by("-created_at")
        context["invite_form"] = forms.get("invite_form") or InviteForm()
    if context["can_change_role"]:
        context["role_form"] = ChangeRoleForm()
    if context["can_extend"]:
        context["extend_form"] = ExtendForm()
    return context


class TeamView(ThesisPermissionRequiredMixin, TemplateView):
    required_action = Action.VIEW_TEAM
    template_name = "accounts/team.html"

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        context.update(team_context(self.request, self.thesis))
        return context


class CsiFileView(ThesisPermissionRequiredMixin, TemplateView):
    """Landing page of CSI members. Placeholder until M8 brings the CSI file."""

    required_action = Action.CSI_FILE_READ
    template_name = "accounts/csi_file.html"

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        context["thesis"] = self.thesis
        return context
