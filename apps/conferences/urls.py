"""Conference routes, mounted under /t/<uuid:thesis_id>/conferences/.

Every view is guarded by permissions.py (tests/test_thesis_routes.py walks them).
Ids only in URLs: never a submission's title.
"""

from django.urls import path

from . import views

app_name = "conferences"

urlpatterns = [
    path("", views.DeadlinesView.as_view(), name="deadlines"),
    path("policy/", views.PolicyView.as_view(), name="policy"),
    path("editions/<uuid:edition_id>/", views.EditionView.as_view(), name="edition"),
    path("submissions/<uuid:submission_id>/", views.SubmissionView.as_view(), name="submission"),
]
