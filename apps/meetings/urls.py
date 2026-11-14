"""Meetings routes, mounted under /t/<uuid:thesis_id>/meetings/.

Every view is guarded by permissions.py (tests/test_thesis_routes.py walks them).
Ids only in URLs: never a meeting's title.
"""

from django.urls import path

from . import views

app_name = "meetings"

M = "<uuid:meeting_id>/"

urlpatterns = [
    path("", views.ListView.as_view(), name="list"),
    path("new/", views.NewView.as_view(), name="new"),
    path("decisions/", views.RegisterView.as_view(), name="register"),
    path(
        "objective/<uuid:objective_id>/actions/",
        views.ObjectiveActionsView.as_view(),
        name="objective_actions",
    ),
    path(M, views.DetailView.as_view(), name="detail"),
    path(M + "edit/", views.EditView.as_view(), name="edit"),
    path(M + "duplicate/", views.DuplicateView.as_view(), name="duplicate"),
    path(M + "meeting.ics", views.IcsView.as_view(), name="ics"),
    path(M + "cancel/", views.CancelView.as_view(), name="cancel"),
    path(M + "level/", views.LevelView.as_view(), name="level"),
    path(M + "agenda/", views.AgendaView.as_view(), name="agenda"),
    path(M + "agenda/prefill/", views.PrefillView.as_view(), name="prefill"),
    path(M + "minutes/", views.MinutesView.as_view(), name="minutes"),
    path(M + "minutes/read/", views.ReadView.as_view(), name="read"),
    path(M + "minutes/approve/", views.ApproveView.as_view(), name="approve"),
    path(M + "decisions/", views.DecisionView.as_view(), name="decision"),
    path(
        M + "decisions/<uuid:decision_id>/withdraw/",
        views.WithdrawDecisionView.as_view(),
        name="withdraw_decision",
    ),
    path(M + "actions/", views.ActionView.as_view(), name="action"),
    path(
        M + "actions/<uuid:action_id>/status/",
        views.ActionStatusView.as_view(),
        name="action_status",
    ),
    path(M + "documents/", views.DocumentView.as_view(), name="document"),
    path(
        M + "documents/<uuid:link_id>/unlink/",
        views.UnlinkDocumentView.as_view(),
        name="unlink_document",
    ),
]
