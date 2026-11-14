"""Administrative-file routes, mounted under /t/<uuid:thesis_id>/admin-file/.

Every view is guarded by permissions.py (tests/test_thesis_routes.py walks them).
"""

from django.urls import path

from . import views

app_name = "admin_file"

urlpatterns = [
    path("", views.FileView.as_view(), name="file"),
    path("new/", views.NewItemView.as_view(), name="new_item"),
    path("<uuid:item_id>/", views.ItemView.as_view(), name="item"),
    path("<uuid:item_id>/submit/", views.SubmitView.as_view(), name="submit"),
    path("<uuid:item_id>/withdraw/", views.WithdrawView.as_view(), name="withdraw"),
    path("<uuid:item_id>/due-date/", views.DueDateView.as_view(), name="due_date"),
    path(
        "<uuid:item_id>/due-date/custom/",
        views.CustomDueDateView.as_view(),
        name="custom_due_date",
    ),
    path(
        "<uuid:item_id>/due-date/propose/",
        views.ProposeDueDateView.as_view(),
        name="propose_due_date",
    ),
    path(
        "<uuid:item_id>/due-date/proposals/<uuid:proposal_id>/accept/",
        views.AcceptDueDateView.as_view(),
        name="accept_due_date",
    ),
    path(
        "<uuid:item_id>/due-date/proposals/<uuid:proposal_id>/refuse/",
        views.RefuseDueDateView.as_view(),
        name="refuse_due_date",
    ),
    path(
        "<uuid:item_id>/due-date/proposals/<uuid:proposal_id>/withdraw/",
        views.WithdrawDueDateView.as_view(),
        name="withdraw_due_date",
    ),
    path("<uuid:item_id>/approve/", views.ApproveView.as_view(), name="approve"),
    path("<uuid:item_id>/reject/", views.RejectView.as_view(), name="reject"),
    path("<uuid:item_id>/reopen/", views.ReopenView.as_view(), name="reopen"),
]
