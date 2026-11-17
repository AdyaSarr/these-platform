from django.urls import path

from . import views

app_name = "activity"

urlpatterns = [
    path("", views.FeedView.as_view(), name="feed"),
    path("read-all/", views.ReadAllView.as_view(), name="read_all"),
    path("preferences/", views.PreferencesView.as_view(), name="preferences"),
    path("<uuid:event_id>/", views.OpenView.as_view(), name="open"),
]
