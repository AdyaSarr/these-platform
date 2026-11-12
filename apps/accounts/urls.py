"""Thesis routes, mounted under /t/<uuid:thesis_id>/ (config/urls.py).

Every view here must be guarded by permissions.py: tests/test_thesis_routes.py
walks these routes and fails otherwise.
"""

from django.urls import path

from . import views

app_name = "accounts"

urlpatterns = [
    path("", views.ThesisDashboardView.as_view(), name="dashboard"),
    path("team/", views.TeamView.as_view(), name="team"),
    path("csi/", views.CsiFileView.as_view(), name="csi_file"),
]
