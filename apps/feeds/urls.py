from django.urls import path

from . import views

app_name = "feeds"

PREFIX = "t/<uuid:thesis_id>/calendar-feed/"

urlpatterns = [
    # The feed itself: outside /t/ (the token alone names the thesis), GET and HEAD.
    path("ical/<str:token>.ics", views.feed, name="ical"),
    path(PREFIX + "create/", views.CreateView.as_view(), name="create"),
    path(PREFIX + "regenerate/", views.RegenerateView.as_view(), name="regenerate"),
    path(PREFIX + "revoke/", views.RevokeView.as_view(), name="revoke"),
]
