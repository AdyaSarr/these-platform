from django.urls import path

from . import views

app_name = "ui"

urlpatterns = [
    path("theme/", views.set_theme, name="theme"),
    path("styleguide/", views.styleguide, name="styleguide"),
]
