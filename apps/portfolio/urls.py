"""Routes of the portfolio (M9a), mounted under /t/<uuid:thesis_id>/portfolio/."""

from django.urls import path

from . import views

app_name = "portfolio"

urlpatterns = [
    path("", views.TrainingsView.as_view(), name="trainings"),
    path("new/", views.NewTrainingView.as_view(), name="new"),
    path("<uuid:training_id>/", views.DetailView.as_view(), name="detail"),
    path("<uuid:training_id>/edit/", views.EditView.as_view(), name="edit"),
    path("<uuid:training_id>/level/", views.LevelView.as_view(), name="level"),
    path("<uuid:training_id>/adum/", views.AdumStatusView.as_view(), name="adum_status"),
    path("adum/", views.AdumView.as_view(), name="adum"),
    path("adum/csv/", views.AdumCsvView.as_view(), name="adum_csv"),
    path("settings/", views.SettingsView.as_view(), name="settings"),
]
