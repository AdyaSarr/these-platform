"""Thesis routes, mounted under /t/<uuid:thesis_id>/ (config/urls.py).

Every view here must be guarded by permissions.py: tests/test_thesis_routes.py
walks these routes and fails otherwise.
"""

from django.urls import path

from . import team_views, views

app_name = "accounts"

urlpatterns = [
    path("", views.ThesisDashboardView.as_view(), name="dashboard"),
    path("team/", views.TeamView.as_view(), name="team"),
    path("csi/", views.CsiFileView.as_view(), name="csi_file"),
    # Team changes: POST only, second factor less than 10 min old (team_views.py).
    path("team/invite/", team_views.InviteView.as_view(), name="team_invite"),
    path(
        "team/<uuid:membership_id>/role/",
        team_views.ChangeRoleView.as_view(),
        name="team_change_role",
    ),
    path(
        "team/<uuid:membership_id>/remove/",
        team_views.RemoveMemberView.as_view(),
        name="team_remove",
    ),
    path(
        "team/<uuid:membership_id>/extend/",
        team_views.ExtendMembershipView.as_view(),
        name="team_extend",
    ),
    path(
        "team/invitations/<uuid:invitation_id>/resend/",
        team_views.ResendInvitationView.as_view(),
        name="team_resend_invitation",
    ),
    path(
        "team/invitations/<uuid:invitation_id>/revoke/",
        team_views.RevokeInvitationView.as_view(),
        name="team_revoke_invitation",
    ),
]
