"""Travel routes, mounted under /t/<uuid:thesis_id>/travel/.

Every view is guarded by permissions.py (tests/test_thesis_routes.py walks them).
Ids only in URLs: never a text of a mission.
"""

from django.urls import path

from . import views

app_name = "travel"

M = "missions/<uuid:mission_id>/"

urlpatterns = [
    path("", views.CalendarView.as_view(), name="calendar"),
    path("<int:year>/<int:month>/", views.MonthView.as_view(), name="month"),
    path("presence/", views.PresenceView.as_view(), name="presence"),
    path("split/", views.SplitView.as_view(), name="split"),
    path("settings/", views.SettingsView.as_view(), name="settings"),
    path("settings/rules/", views.RulesView.as_view(), name="rules"),
    path("settings/target/", views.TargetView.as_view(), name="target"),
    path("settings/reminders/", views.RemindersView.as_view(), name="reminders"),
    path("missions/", views.MissionsView.as_view(), name="missions"),
    path("missions/new/", views.NewMissionView.as_view(), name="new_mission"),
    path(M, views.MissionView.as_view(), name="mission"),
    path(M + "edit/", views.EditMissionView.as_view(), name="edit_mission"),
    path(M + "level/", views.MissionLevelView.as_view(), name="mission_level"),
    path(M + "cancel/", views.CancelMissionView.as_view(), name="cancel_mission"),
    path(M + "done/", views.MissionDoneView.as_view(), name="mission_done"),
]
