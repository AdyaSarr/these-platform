"""Travel routes, mounted under /t/<uuid:thesis_id>/travel/.

Every view is guarded by permissions.py (tests/test_thesis_routes.py walks them).
"""

from django.urls import path

from . import views

app_name = "travel"

urlpatterns = [
    path("", views.CalendarView.as_view(), name="calendar"),
    path("<int:year>/<int:month>/", views.MonthView.as_view(), name="month"),
    path("presence/", views.PresenceView.as_view(), name="presence"),
    path("split/", views.SplitView.as_view(), name="split"),
    path("settings/", views.SettingsView.as_view(), name="settings"),
    path("settings/rules/", views.RulesView.as_view(), name="rules"),
    path("settings/target/", views.TargetView.as_view(), name="target"),
    path("settings/reminders/", views.RemindersView.as_view(), name="reminders"),
]
