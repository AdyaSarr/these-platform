"""Routes of the export (M13b), mounted under /t/<uuid:thesis_id>/export/."""

from django.urls import path

from . import views

app_name = "export"

urlpatterns = [
    path("", views.PageView.as_view(), name="page"),
    path("download/", views.DownloadView.as_view(), name="download"),
    path("st/", views.StPageView.as_view(), name="st"),
    path("st/date/", views.StDateView.as_view(), name="st_date"),
    path("st/grant/", views.StGrantView.as_view(), name="st_grant"),
    path("st/<uuid:consent_id>/revoke/", views.StRevokeView.as_view(), name="st_revoke"),
]
