"""Every page and htmx fragment of the objectives, including 400 and 409 answers,
renders without inline script, inline style or on* handler (strict CSP)."""

from __future__ import annotations

import datetime as dt
import secrets
from typing import Any

import pytest
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from apps.accounts.roles import Role
from apps.accounts.tests.factories import MembershipFactory, ThesisFactory
from apps.accounts.tests.helpers import mfa_login
from apps.objectives import services
from apps.objectives.models import Status
from apps.objectives.tests.helpers import D
from tests.test_csp_rendered_pages import _assert_csp_clean

pytestmark = pytest.mark.django_db(transaction=True)

PASSWORD = secrets.token_urlsafe(16)
HX = {"HX-Request": "true"}


def test_pages_and_fragments_are_csp_clean(time_machine: Any) -> None:
    time_machine.move_to(dt.datetime(2027, 3, 15, 10, 0, tzinfo=dt.UTC), tick=True)
    thesis = ThesisFactory.create(
        cifre_start_date=D(2026, 12, 1),
        first_enrollment_date=D(2026, 12, 15),
        planned_end_date=D(2029, 11, 30),
    )

    def member(role: Role, **kwargs: Any) -> Any:
        return MembershipFactory.create(
            thesis=thesis, role=role, user__password=PASSWORD, **kwargs
        ).user

    student, director = member(Role.PHD_STUDENT), member(Role.DIRECTOR)
    csi = member(Role.CSI_MEMBER, active_until=timezone.now() + dt.timedelta(days=60))
    year = thesis.years.get(number=1)
    objective = services.create_objective(
        thesis=thesis,
        user=student,
        year=year,
        title="Titre fictif",
        category="RESEARCH",
        due_date=D(2027, 3, 1),
    )
    draft = services.create_objective(
        thesis=thesis,
        user=student,
        year=year,
        title="Brouillon fictif",
        category="TRAINING",
        due_date=D(2027, 9, 1),
    )
    services.approve_objective(objective=objective, user=director, recent_mfa=True)
    services.add_progress(
        objective=objective,
        user=student,
        date=D(2027, 3, 10),
        progress=40,
        status=Status.IN_PROGRESS,
        note="Note fictive",
    )
    services.add_comment(objective=objective, user=director, body="Avis fictif")

    def url(name: str, **kw: Any) -> str:
        return reverse(f"objectives:{name}", kwargs={"thesis_id": thesis.pk, **kw})

    detail = url("detail", objective_id=objective.pk)
    as_student = Client()
    as_student.force_login(student)
    pages = {
        "dashboard": as_student.get(url("dashboard")),
        "new": as_student.get(url("new")),
        "new_400": as_student.post(url("new"), {"title": ""}),
        "detail": as_student.get(detail),
        "edit": as_student.get(url("edit", objective_id=objective.pk)),
        "edit_400": as_student.post(url("edit", objective_id=objective.pk), {"title": "x"}),
        "drop_400": as_student.post(url("drop", objective_id=objective.pk), {"reason": ""}),
        "progress_fragment": as_student.post(
            url("progress", objective_id=objective.pk),
            {"date": "2027-03-12", "progress": "50", "status": "IN_PROGRESS", "note": "n"},
            headers=HX,
        ),
        "progress_400": as_student.post(
            url("progress", objective_id=objective.pk), {"progress": "500"}, headers=HX
        ),
        "comments_fragment": as_student.get(url("comments", objective_id=objective.pk), headers=HX),
        "comments_400": as_student.post(
            url("comments", objective_id=objective.pk), {"body": ""}, headers=HX
        ),
        "thesis_dashboard": as_student.get(
            reverse("accounts:dashboard", kwargs={"thesis_id": thesis.pk})
        ),
    }
    as_director = Client()
    mfa_login(as_director, director, PASSWORD)
    pages["director_dashboard"] = as_director.get(url("dashboard"))
    pages["director_detail"] = as_director.get(detail)
    pages["approve_409"] = as_director.post(url("approve", objective_id=objective.pk))
    pages["acknowledge_409"] = as_director.post(url("acknowledge", objective_id=objective.pk))
    pages["approve_year_400"] = as_director.post(url("approve_year"), {"year": "9"})
    pages["approve_draft"] = as_director.post(url("approve", objective_id=draft.pk), follow=True)
    as_csi = Client()
    as_csi.force_login(csi)
    pages["csi_dashboard"] = as_csi.get(url("dashboard"))
    pages["csi_detail"] = as_csi.get(detail)
    seen = set()
    for name, response in pages.items():
        assert response.status_code in (200, 400, 409), (name, response.status_code)
        seen.add(response.status_code)
        _assert_csp_clean(response.content.decode(), name)
    assert seen == {200, 400, 409}
