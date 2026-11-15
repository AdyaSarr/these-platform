"""Travel routes, mounted under /t/<uuid:thesis_id>/travel/.

Every view is guarded by permissions.py (tests/test_thesis_routes.py walks them).
Ids only in URLs: never a text of a mission.
"""

from django.urls import path

from . import views

app_name = "travel"

MISSION = "missions/<uuid:mission_id>/"
ORDER = "orders/<uuid:order_id>/"

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
    path(MISSION, views.MissionView.as_view(), name="mission"),
    path(MISSION + "edit/", views.EditMissionView.as_view(), name="edit_mission"),
    path(MISSION + "level/", views.MissionLevelView.as_view(), name="mission_level"),
    path(MISSION + "cancel/", views.CancelMissionView.as_view(), name="cancel_mission"),
    path(MISSION + "done/", views.MissionDoneView.as_view(), name="mission_done"),
    path(MISSION + "order/", views.RequestOrderView.as_view(), name="request_order"),
    path("orders/", views.OrdersView.as_view(), name="orders"),
    path(ORDER + "decide/", views.DecideOrderView.as_view(), name="decide_order"),
    path(ORDER + "withdraw/", views.WithdrawOrderView.as_view(), name="withdraw_order"),
]
