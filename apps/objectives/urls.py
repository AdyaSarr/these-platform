"""Objectives routes, mounted under /t/<uuid:thesis_id>/objectives/.

Every view is guarded by permissions.py (tests/test_thesis_routes.py walks them).
Ids only in URLs: never an objective's title.
"""

from django.urls import path

from . import views

app_name = "objectives"

urlpatterns = [
    path("", views.DashboardView.as_view(), name="dashboard"),
    path("new/", views.NewObjectiveView.as_view(), name="new"),
    path("approve-year/", views.ApproveYearView.as_view(), name="approve_year"),
    path("<uuid:objective_id>/", views.DetailView.as_view(), name="detail"),
    path("<uuid:objective_id>/edit/", views.EditView.as_view(), name="edit"),
    path("<uuid:objective_id>/level/", views.LevelView.as_view(), name="level"),
    path("<uuid:objective_id>/drop/", views.DropView.as_view(), name="drop"),
    path("<uuid:objective_id>/progress/", views.ProgressView.as_view(), name="progress"),
    path("<uuid:objective_id>/comments/", views.CommentsView.as_view(), name="comments"),
    path("<uuid:objective_id>/approve/", views.ApproveView.as_view(), name="approve"),
    path("<uuid:objective_id>/acknowledge/", views.AcknowledgeView.as_view(), name="acknowledge"),
]
