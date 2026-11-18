"""Routes of the end of the thesis, mounted under /t/<uuid:thesis_id>/lifecycle/."""

from django.urls import path

from . import views

app_name = "lifecycle"

urlpatterns = [
    path("", views.PageView.as_view(), name="page"),
    path("defend/", views.DefendView.as_view(), name="defend"),
    path("cancel/", views.CancelView.as_view(), name="cancel"),
    path("request/", views.RequestView.as_view(), name="request"),
    path("requests/<uuid:request_id>/confirm/", views.ConfirmView.as_view(), name="confirm"),
    path("requests/<uuid:request_id>/withdraw/", views.WithdrawView.as_view(), name="withdraw"),
]
