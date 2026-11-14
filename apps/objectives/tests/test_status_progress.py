"""Status and progress kept consistent: a progress above 0 % is never « Prévu ».

- the progress form is pre-filled with the objective's current status and progress;
- PLANNED with a progress > 0 becomes IN_PROGRESS in the same transaction (not a
  result: no mark, no acknowledgement);
- going back to PLANNED with a progress > 0 is refused;
- 100 % without ACHIEVED stays IN_PROGRESS and shows « à déclarer atteint » to the
  student; ACHIEVED stays the student's explicit decision (marked, acknowledged);
- the database refuses PLANNED with a progress > 0 (CHECK), after a data migration
  that moved the existing rows to IN_PROGRESS.
"""

from __future__ import annotations

import datetime as dt
import html
import re
from types import SimpleNamespace
from typing import Any

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError, connection, transaction
from django.db.migrations.executor import MigrationExecutor
from django.test import Client
from django.urls import reverse

from apps.objectives import services
from apps.objectives.models import Objective, ObjectiveRevision, ProgressUpdate, Status
from apps.objectives.tests.helpers import D, approved, make_team, objective_of
from apps.vault.models import AuditEvent

pytestmark = pytest.mark.django_db(transaction=True)

NOW = dt.datetime(2027, 3, 15, 10, 0, tzinfo=dt.UTC)
TO_DECLARE = "100 % — à déclarer atteint"


@pytest.fixture(autouse=True)
def _frozen(time_machine: Any) -> None:
    time_machine.move_to(NOW, tick=False)


@pytest.fixture
def team() -> SimpleNamespace:
    return make_team()


def _report(team: SimpleNamespace, objective: Objective, progress: int, status: str) -> Any:
    return services.add_progress(
        objective=objective,
        user=team.student,
        date=D(2027, 3, 10),
        progress=progress,
        status=status,
    )


# --------------------------------------------------------------------------- service


def test_a_progress_above_zero_moves_planned_to_in_progress(team: SimpleNamespace) -> None:
    objective = approved(team)
    update = _report(team, objective, 30, Status.PLANNED)
    row = Objective.objects.get(pk=objective.pk)
    assert (row.status, row.progress) == (Status.IN_PROGRESS, 30)
    assert update.status == Status.IN_PROGRESS  # the report records what was applied
    assert not row.result_declared and not row.changed_since_approval  # not a result
    assert not ObjectiveRevision.objects.exists()
    (event,) = AuditEvent.objects.filter(action="OBJECTIVE_PROGRESS_ADDED")
    assert event.details_json["status"] == "IN_PROGRESS"


def test_zero_percent_planned_stays_planned(team: SimpleNamespace) -> None:
    objective = approved(team)
    _report(team, objective, 0, Status.PLANNED)
    assert Objective.objects.get(pk=objective.pk).status == Status.PLANNED


def test_going_back_to_planned_with_a_progress_is_refused(team: SimpleNamespace) -> None:
    objective = approved(team)
    _report(team, objective, 40, Status.IN_PROGRESS)
    with pytest.raises(ValidationError) as caught:
        _report(team, objective, 50, Status.PLANNED)
    assert caught.value.code == "planned_with_progress"
    row = Objective.objects.get(pk=objective.pk)
    assert (row.status, row.progress) == (Status.IN_PROGRESS, 40)
    assert ProgressUpdate.objects.filter(objective=objective).count() == 1


def test_going_back_to_planned_at_zero_is_allowed(team: SimpleNamespace) -> None:
    objective = approved(team)
    _report(team, objective, 40, Status.IN_PROGRESS)
    _report(team, objective, 0, Status.PLANNED)
    row = Objective.objects.get(pk=objective.pk)
    assert (row.status, row.progress) == (Status.PLANNED, 0)


def test_one_hundred_percent_is_not_achieved(team: SimpleNamespace) -> None:
    objective = approved(team)
    _report(team, objective, 100, Status.IN_PROGRESS)
    row = Objective.objects.get(pk=objective.pk)
    assert (row.status, row.progress) == (Status.IN_PROGRESS, 100)
    assert not row.result_declared


def test_achieved_stays_an_explicit_decision(team: SimpleNamespace) -> None:
    objective = approved(team)
    _report(team, objective, 60, Status.ACHIEVED)
    row = Objective.objects.get(pk=objective.pk)
    assert (row.status, row.progress, row.result_declared) == (Status.ACHIEVED, 100, True)


# --------------------------------------------------------------------------- pages


def _page(team: SimpleNamespace, role: str, objective: Objective) -> str:
    client = Client()
    client.force_login(getattr(team, role))
    response = client.get(
        reverse(
            "objectives:detail", kwargs={"thesis_id": team.thesis.pk, "objective_id": objective.pk}
        )
    )
    assert response.status_code == 200
    return re.sub(r"\s+", " ", html.unescape(response.content.decode()))


def test_the_form_is_prefilled_with_the_current_status_and_progress(team: SimpleNamespace) -> None:
    objective = approved(team)
    _report(team, objective, 40, Status.IN_PROGRESS)
    page = _page(team, "student", objective)
    assert re.search(r'<option value="IN_PROGRESS" selected>', page)
    assert not re.search(r'<option value="PLANNED" selected>', page)
    assert re.search(r'<input type="number" name="progress" value="40"', page)


def test_one_hundred_percent_shows_to_declare_to_the_student_only(team: SimpleNamespace) -> None:
    objective = approved(team)
    _report(team, objective, 100, Status.IN_PROGRESS)
    assert TO_DECLARE in _page(team, "student", objective)
    assert TO_DECLARE not in _page(team, "director", objective)


def test_the_dashboard_list_shows_to_declare_to_the_student(team: SimpleNamespace) -> None:
    objective = approved(team)
    _report(team, objective, 100, Status.IN_PROGRESS)
    client = Client()
    client.force_login(team.student)
    page = html.unescape(
        client.get(
            reverse("objectives:dashboard", kwargs={"thesis_id": team.thesis.pk})
        ).content.decode()
    )
    assert "à déclarer atteint" in page


def test_the_view_refuses_planned_with_a_progress(team: SimpleNamespace) -> None:
    objective = approved(team)
    _report(team, objective, 40, Status.IN_PROGRESS)
    client = Client()
    client.force_login(team.student)
    url = reverse(
        "objectives:progress", kwargs={"thesis_id": team.thesis.pk, "objective_id": objective.pk}
    )
    response = client.post(
        url,
        {"date": "2027-03-12", "progress": "50", "status": "PLANNED"},
        headers={"HX-Request": "true"},
    )
    assert response.status_code == 400
    assert "Prévu" in html.unescape(response.content.decode())


# --------------------------------------------------------------------------- database


def test_the_database_refuses_planned_with_a_progress(team: SimpleNamespace) -> None:
    objective = objective_of(team)
    with pytest.raises(IntegrityError), transaction.atomic():
        Objective.objects.filter(pk=objective.pk).update(progress=50)  # still PLANNED


def test_the_data_migration_moves_existing_rows_to_in_progress(team: SimpleNamespace) -> None:
    before_constraint = [("objectives", "0003_weekly_summary")]
    planned = objective_of(team)
    untouched = objective_of(team)
    executor = MigrationExecutor(connection)
    executor.migrate(before_constraint)  # no CHECK yet: write a row as the old code could
    Objective.objects.filter(pk=planned.pk).update(progress=70)
    executor = MigrationExecutor(connection)
    executor.migrate(executor.loader.graph.leaf_nodes())
    assert Objective.objects.get(pk=planned.pk).status == Status.IN_PROGRESS
    assert Objective.objects.get(pk=planned.pk).progress == 70
    assert Objective.objects.get(pk=untouched.pk).status == Status.PLANNED
    moved = Objective.objects.get(pk=planned.pk)
    assert not moved.result_declared and not moved.changed_since_approval
    assert not ObjectiveRevision.objects.exists()
