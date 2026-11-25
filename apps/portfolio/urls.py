"""Routes of the portfolio (M9a, M9b), mounted under /t/<uuid:thesis_id>/portfolio/."""

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
    path("adum/bibtex/", views.AdumBibtexView.as_view(), name="adum_bibtex"),
    path("settings/", views.SettingsView.as_view(), name="settings"),
    # M9b: publication details of the submissions of M6
    path("publications/", views.PublicationsView.as_view(), name="publications"),
    path("publications/<uuid:submission_id>/", views.PublicationView.as_view(), name="publication"),
    path(
        "publications/<uuid:submission_id>/edit/",
        views.EditPublicationView.as_view(),
        name="edit_publication",
    ),
    # M9b: teaching and outreach
    path("teaching/", views.TeachingsView.as_view(), name="teachings"),
    path("teaching/new/", views.NewTeachingView.as_view(), name="new_teaching"),
    path("teaching/<uuid:teaching_id>/", views.TeachingView.as_view(), name="teaching"),
    path(
        "teaching/<uuid:teaching_id>/edit/", views.EditTeachingView.as_view(), name="edit_teaching"
    ),
    path(
        "teaching/<uuid:teaching_id>/level/",
        views.TeachingLevelView.as_view(),
        name="teaching_level",
    ),
    path("outreach/", views.OutreachListView.as_view(), name="outreach_list"),
    path("outreach/new/", views.NewOutreachView.as_view(), name="new_outreach"),
    path("outreach/<uuid:outreach_id>/", views.OutreachView.as_view(), name="outreach"),
    path(
        "outreach/<uuid:outreach_id>/edit/", views.EditOutreachView.as_view(), name="edit_outreach"
    ),
    path(
        "outreach/<uuid:outreach_id>/level/",
        views.OutreachLevelView.as_view(),
        name="outreach_level",
    ),
    # M9b: the student's skills and professional project (the student alone)
    path("skills/", views.SkillsView.as_view(), name="skills"),
    path("skills/new/", views.NewSkillView.as_view(), name="new_skill"),
    path("skills/<uuid:skill_id>/edit/", views.EditSkillView.as_view(), name="edit_skill"),
    path("career/", views.CareerView.as_view(), name="career"),
]
