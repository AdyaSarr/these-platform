"""Conference routes, mounted under /t/<uuid:thesis_id>/conferences/.

Every view is guarded by permissions.py (tests/test_thesis_routes.py walks them).
Ids only in URLs: never a submission's title.
"""

from django.urls import path

from . import views

app_name = "conferences"

S = "submissions/<uuid:submission_id>/"

urlpatterns = [
    path("", views.DeadlinesView.as_view(), name="deadlines"),
    path("policy/", views.PolicyView.as_view(), name="policy"),
    path("policy/edit/", views.EditPolicyView.as_view(), name="policy_edit"),
    path("venues/new/", views.NewVenueView.as_view(), name="new_venue"),
    path("venues/<uuid:venue_id>/", views.VenueView.as_view(), name="venue"),
    path("venues/<uuid:venue_id>/editions/", views.NewEditionView.as_view(), name="new_edition"),
    path("editions/<uuid:edition_id>/", views.EditionView.as_view(), name="edition"),
    path(
        "deadlines/<uuid:deadline_id>/cancel/",
        views.CancelDeadlineView.as_view(),
        name="cancel_deadline",
    ),
    path("submissions/", views.SubmissionsView.as_view(), name="submissions"),
    path("submissions/new/", views.NewSubmissionView.as_view(), name="new_submission"),
    path(
        "objective/<uuid:objective_id>/submissions/",
        views.ObjectiveSubmissionsView.as_view(),
        name="objective_submissions",
    ),
    path(S, views.SubmissionView.as_view(), name="submission"),
    path(S + "edit/", views.EditSubmissionView.as_view(), name="edit_submission"),
    path(S + "documents/", views.DocumentView.as_view(), name="document"),
    path(
        S + "documents/<uuid:link_id>/unlink/",
        views.UnlinkDocumentView.as_view(),
        name="unlink_document",
    ),
    path(S + "approvals/", views.RequestView.as_view(), name="request"),
    path(
        S + "approvals/<uuid:approval_id>/withdraw/",
        views.WithdrawApprovalView.as_view(),
        name="withdraw_approval",
    ),
    path(S + "approvals/<uuid:approval_id>/decide/", views.DecideView.as_view(), name="decide"),
    path(S + "status/", views.StatusView.as_view(), name="status"),
    path(S + "preprints/", views.PreprintView.as_view(), name="preprint"),
    path(S + "level/", views.LevelView.as_view(), name="level"),
    path(S + "st-work/off/", views.StWorkOffView.as_view(), name="st_work_off"),
    path(S + "st-work/on/", views.StWorkOnView.as_view(), name="st_work_on"),
    path(S + "resubmit/", views.ResubmitView.as_view(), name="resubmit"),
]
