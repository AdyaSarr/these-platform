"""Expenses of a mission (M7, step 5): lines by category with sealed estimated and actual
amounts; a flat-rate line (MEALS, OTHER only) needs no receipt; receipts stored in the
vault at the mission's level, under a neutral name (never the uploaded file's name:
not in the database, the journal, nor the download header); "reimbursed" needs an
actual amount on every line and a receipt on every line that is not flat-rate."""

from __future__ import annotations

import datetime as dt
from types import SimpleNamespace
from typing import Any

import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse

from apps.accounts.permissions import can_access_document
from apps.core.confidentiality import ConfidentialityLevel as L
from apps.travel import services
from apps.travel.models import Expense, Mission
from apps.travel.tests.helpers import NOW, database_dump, make_team, marker, mission_of
from apps.vault import services as vault
from apps.vault.models import AuditEvent, Document, DocumentVersion

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture(autouse=True)
def _now(time_machine: Any) -> None:
    time_machine.move_to(NOW, tick=True)


@pytest.fixture
def team() -> SimpleNamespace:
    return make_team()


def _events(action: str) -> list[dict[str, Any]]:
    return [e.details_json for e in AuditEvent.objects.filter(action=action).order_by("seq")]


def _pdf(name: str = "facture.pdf") -> SimpleUploadedFile:
    return SimpleUploadedFile(name, b"%PDF-1.7\n%justificatif fictif\n%%EOF\n", "application/pdf")


def _line(team: SimpleNamespace, mission: Mission, **kw: Any) -> Expense:
    fields: dict[str, Any] = {"category": "TRANSPORT", "estimated": "120"}
    fields.update(kw)
    return services.add_expense(mission=mission, user=team.student, **fields)


def _view(user: Any, mission: Mission) -> list[services.ExpenseView]:
    return services.expenses_of(user, mission)


# =========================================================================== lines


def test_the_student_adds_lines_with_sealed_amounts(team: SimpleNamespace) -> None:
    mission = mission_of(team)
    line = _line(team, mission, estimated="123,45", actual="")
    (view,) = _view(team.student, mission)
    assert (view.estimated, view.actual) == (12345, None)
    assert "123.45" not in "\n".join(database_dump())
    assert _events("EXPENSE_ADDED") == [
        {"expense_id": str(line.pk), "category": "TRANSPORT", "flat_rate": False}
    ]


@pytest.mark.parametrize("role", ["director", "co_supervisor", "tutor", "csi"])
def test_only_the_student_adds_lines(team: SimpleNamespace, role: str) -> None:
    mission = mission_of(team)
    with pytest.raises(PermissionDenied):
        services.add_expense(mission=mission, user=getattr(team, role), category="MEALS")
    assert not Expense.objects.exists()


@pytest.mark.parametrize(
    "fields",
    [{"category": "TAXI"}, {"estimated": "1e3"}, {"actual": "-2"}, {"estimated": "\uff11"}],
)
def test_invalid_lines(team: SimpleNamespace, fields: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        _line(team, mission_of(team), **fields)
    assert not Expense.objects.exists()


def test_amounts_are_updated_and_journaled_without_amount(team: SimpleNamespace) -> None:
    mission = mission_of(team)
    line = _line(team, mission)
    changed = services.update_expense(expense=line, user=team.student, actual="118,20")
    assert changed == ["actual"]
    (view,) = _view(team.student, mission)
    assert (view.estimated, view.actual) == (12000, 11820)
    assert _events("EXPENSE_UPDATED") == [{"expense_id": str(line.pk), "fields": ["actual"]}]
    assert "118" not in str(_events("EXPENSE_UPDATED"))


def test_an_amount_can_be_cleared(team: SimpleNamespace) -> None:
    mission = mission_of(team)
    line = _line(team, mission, estimated="10")
    services.update_expense(expense=line, user=team.student, estimated="")
    (view,) = _view(team.student, mission)
    assert view.estimated is None


@pytest.mark.parametrize("status", ["CANCELLED", "REIMBURSED"])
def test_no_line_on_a_closed_mission(team: SimpleNamespace, status: str) -> None:
    mission = mission_of(team)
    Mission.objects.filter(pk=mission.pk).update(status=status)
    with pytest.raises(ValidationError) as info:
        _line(team, mission)
    assert info.value.code == "terminal"


def test_a_done_mission_still_takes_lines(team: SimpleNamespace) -> None:
    mission = mission_of(team)
    Mission.objects.filter(pk=mission.pk).update(status="DONE")
    _line(team, mission, actual="12")


# =========================================================================== flat rate


@pytest.mark.parametrize("category", ["MEALS", "OTHER"])
def test_a_flat_rate_line(team: SimpleNamespace, category: str) -> None:
    line = _line(team, mission_of(team), category=category, flat_rate=True)
    assert line.flat_rate is True
    assert _events("EXPENSE_ADDED")[0]["flat_rate"] is True


@pytest.mark.parametrize("category", ["TRANSPORT", "LODGING", "REGISTRATION"])
def test_flat_rate_only_for_meals_and_other(team: SimpleNamespace, category: str) -> None:
    with pytest.raises(ValidationError) as info:
        _line(team, mission_of(team), category=category, flat_rate=True)
    assert info.value.code == "flat_rate"


def test_flat_rate_changes_are_journaled(team: SimpleNamespace) -> None:
    line = _line(team, mission_of(team), category="MEALS")
    assert services.update_expense(expense=line, user=team.student, flat_rate=True) == ["flat_rate"]
    assert _events("EXPENSE_UPDATED")[-1] == {"expense_id": str(line.pk), "fields": ["flat_rate"]}
    line_other = _line(team, mission_of(team), category="TRANSPORT")
    with pytest.raises(ValidationError):
        services.update_expense(expense=line_other, user=team.student, flat_rate=True)


# =========================================================================== receipts


def test_a_receipt_is_stored_at_the_missions_level(team: SimpleNamespace) -> None:
    mission = mission_of(team, payer="ST")
    line = _line(team, mission)
    document = services.attach_receipt(expense=line, user=team.student, upload=_pdf())
    assert document.confidentiality == "INTERNAL_ST"
    assert document.title == "Justificatif 2027-02-08"
    (view,) = _view(team.student, mission)
    assert [d.pk for d in view.receipts] == [document.pk]
    assert _events("EXPENSE_RECEIPT_ATTACHED") == [
        {"expense_id": str(line.pk), "document_id": str(document.pk)}
    ]


def test_receipts_are_read_like_the_mission(team: SimpleNamespace) -> None:
    lab = services.attach_receipt(
        expense=_line(team, mission_of(team, payer="LIRMM")), user=team.student, upload=_pdf()
    )
    hidden = services.attach_receipt(
        expense=_line(team, mission_of(team, destination="ROUSSET", level=L.PUBLIC)),
        user=team.student,
        upload=_pdf(),
    )
    assert can_access_document(team.co_supervisor, lab)
    assert not can_access_document(team.co_supervisor, hidden)  # PUBLIC, but hidden mission
    assert not can_access_document(team.tutor, lab)  # INTERNAL_LIRMM


def test_the_uploaded_file_name_never_appears(team: SimpleNamespace) -> None:
    secret = marker()
    mission = mission_of(team, payer="ST")
    receipt = services.attach_receipt(
        expense=_line(team, mission), user=team.student, upload=_pdf(f"{secret}.pdf")
    )
    official = services.attach_official_order(
        mission=mission, user=team.student, upload=_pdf(f"ordre-{secret}.pdf")
    )
    assert secret not in "\n".join(database_dump())  # rows, audit details included
    from apps.travel.tests.helpers import client_for

    client = client_for(team, "student")
    names = {}
    for document in (receipt, official):
        version = DocumentVersion.objects.get(document=document, version=1)
        response = client.get(
            reverse(
                "vault:download", kwargs={"thesis_id": team.thesis.pk, "version_id": version.pk}
            )
        )
        assert response.status_code == 200
        assert secret not in response["Content-Disposition"]
        names[document.pk] = response["Content-Disposition"]
    assert 'filename="justificatif-2027-02-08.pdf"' in names[receipt.pk]
    assert 'filename="ordre-de-mission-officiel-2027-02-08.pdf"' in names[official.pk]


@pytest.mark.parametrize("role", ["director", "tutor"])
def test_only_the_student_attaches_receipts(team: SimpleNamespace, role: str) -> None:
    line = _line(team, mission_of(team))
    with pytest.raises(PermissionDenied):
        services.attach_receipt(expense=line, user=getattr(team, role), upload=_pdf())
    assert not Document.objects.exists()


def test_a_level_change_reseals_amounts_and_moves_receipts(team: SimpleNamespace) -> None:
    mission = mission_of(team, payer="ST")
    line = _line(team, mission, estimated="40", actual="39")
    receipt = services.attach_receipt(expense=line, user=team.student, upload=_pdf())
    (before,) = _view(team.director, mission)
    assert before.estimated is vault.RESTRICTED
    services.change_mission_level(
        mission=mission, user=team.student, level=L.CONFIDENTIAL, recent_mfa=True
    )
    (after,) = _view(team.director, mission)
    assert (after.estimated, after.actual) == (4000, 3900)
    receipt.refresh_from_db()
    assert receipt.confidentiality == "CONFIDENTIAL"


# =========================================================================== reimbursed


def _done(team: SimpleNamespace, time_machine: Any, **kw: Any) -> Mission:
    mission: Mission = mission_of(team, payer="OTHER", **kw)
    time_machine.move_to(dt.datetime(2027, 2, 11, 9, 0, tzinfo=dt.UTC), tick=True)
    services.mark_done(mission=mission, user=team.student)
    return mission


def test_reimbursed_needs_actual_amounts_and_receipts(
    team: SimpleNamespace, time_machine: Any
) -> None:
    mission = _done(team, time_machine)
    transport = _line(team, mission, actual="")
    meals = _line(team, mission, category="MEALS", flat_rate=True, actual="")
    for code, fix in (
        (
            "actual_missing",
            lambda: services.update_expense(expense=transport, user=team.student, actual="80"),
        ),
        (
            "receipt_missing",
            lambda: services.attach_receipt(expense=transport, user=team.student, upload=_pdf()),
        ),
        (
            "actual_missing",
            lambda: services.update_expense(expense=meals, user=team.student, actual="15"),
        ),
    ):
        with pytest.raises(ValidationError) as info:
            services.mark_reimbursed(mission=mission, user=team.student)
        assert info.value.code == code
        fix()
    services.mark_reimbursed(mission=mission, user=team.student)  # the flat-rate line: no receipt
    mission.refresh_from_db()
    assert mission.status == "REIMBURSED"
    assert _events("MISSION_STATUS_CHANGED")[-1] == {"from": "DONE", "to": "REIMBURSED"}


def test_reimbursed_only_after_done(team: SimpleNamespace) -> None:
    mission = mission_of(team, payer="OTHER")
    with pytest.raises(ValidationError) as info:
        services.mark_reimbursed(mission=mission, user=team.student)
    assert info.value.code == "not_done"


@pytest.mark.parametrize("role", ["director", "tutor"])
def test_only_the_student_marks_reimbursed(
    team: SimpleNamespace, time_machine: Any, role: str
) -> None:
    mission = _done(team, time_machine)
    with pytest.raises(PermissionDenied):
        services.mark_reimbursed(mission=mission, user=getattr(team, role))


def test_amounts_follow_the_level(team: SimpleNamespace) -> None:
    mission = mission_of(team, payer="ST")  # INTERNAL_ST
    _line(team, mission, estimated="50")
    (tutor,) = _view(team.tutor, mission)
    (director,) = _view(team.director, mission)
    assert tutor.estimated == 5000
    assert director.estimated is vault.RESTRICTED
    assert director.expense.category == "TRANSPORT"  # the category is not sealed
