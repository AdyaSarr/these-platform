"""Document routes, mounted under /t/<uuid:thesis_id>/documents/ (config/urls.py).

Every view is guarded by permissions.py (tests/test_thesis_routes.py walks them).
"""

from django.urls import path

from . import views

app_name = "vault"

urlpatterns = [
    path("", views.DocumentListView.as_view(), name="documents"),
    path("new/", views.UploadView.as_view(), name="upload"),
    path("<uuid:document_id>/", views.DocumentDetailView.as_view(), name="document"),
    path("<uuid:document_id>/versions/new/", views.NewVersionView.as_view(), name="new_version"),
    path("<uuid:document_id>/level/", views.ChangeLevelView.as_view(), name="change_level"),
    path("<uuid:document_id>/grants/", views.GrantView.as_view(), name="grant"),
    path(
        "<uuid:document_id>/grants/<uuid:grant_id>/revoke/",
        views.RevokeGrantView.as_view(),
        name="revoke_grant",
    ),
    path("versions/<uuid:version_id>/download/", views.DownloadView.as_view(), name="download"),
]
