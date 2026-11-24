"""ItemReopening and decided DueDateProposal rows are history: never modified nor
deleted by the code. Checked twice: statically (no update/delete path in the
application code) and at run time (the models and their querysets refuse)."""

from __future__ import annotations

import ast
import datetime as dt
from pathlib import Path
from types import SimpleNamespace

import pytest

from apps.admin_file import services
from apps.admin_file.models import (
    DueDateProposal,
    DueDateReason,
    HistoryError,
    ItemReopening,
    Reason,
    RequiredItem,
)
from apps.admin_file.tests.helpers import item_of, make_team

APPS = Path(__file__).resolve().parents[2]
HISTORIES = {"ItemReopening", "DueDateProposal"}
WRITES = {
    "update", "delete", "bulk_update", "bulk_create", "update_or_create", "get_or_create",
    "raw", "_raw_delete", "_update",
}  # fmt: skip


def _application_files() -> list[Path]:
    return [p for p in APPS.rglob("*.py") if "tests" not in p.parts and "migrations" not in p.parts]


def _root_name(node: ast.AST) -> str | None:
    while isinstance(node, ast.Attribute | ast.Call | ast.Subscript):
        node = node.func if isinstance(node, ast.Call) else node.value
    return node.id if isinstance(node, ast.Name) else None


def test_no_bulk_write_or_delete_on_a_history_in_the_code() -> None:
    offenders = []
    for path in _application_files():
        for node in ast.walk(ast.parse(path.read_text())):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in WRITES
                and _root_name(node.func) in HISTORIES
            ):
                offenders.append(f"{path.relative_to(APPS)}:{node.lineno}")
    assert offenders == []


def test_histories_are_written_by_the_services_only() -> None:
    users = {
        str(p.relative_to(APPS))
        for p in _application_files()
        if any(name in p.read_text() for name in HISTORIES)
    }
    assert users <= {"admin_file/models.py", "admin_file/services.py", "admin_file/views.py",
                     "admin_file/notifications.py", "demo/scenario.py",
                     # M13b: the export reads them, and its coverage guard names them
                     "export/snapshot.py", "export/coverage.py"}  # fmt: skip
    for path in users - {"admin_file/models.py", "admin_file/services.py"}:
        for node in ast.walk(ast.parse((APPS / path).read_text())):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                assert node.func.attr not in {"save", "create", "delete"} or (
                    _root_name(node.func) not in HISTORIES
                ), f"{path}:{node.lineno}"


# --------------------------------------------------------------------------- at run time


@pytest.fixture
def team() -> SimpleNamespace:
    return make_team()


def _reopening(team: SimpleNamespace) -> ItemReopening:
    item = item_of(team)
    services.submit(item=item, user=team.student)
    services.approve(item=item, user=team.director)
    services.reopen(item=item, user=team.director, reason=Reason.OTHER)
    return ItemReopening.objects.get(item=item)


@pytest.mark.django_db
def test_a_reopening_is_never_modified_nor_deleted(team: SimpleNamespace) -> None:
    row = _reopening(team)
    row.reason_comment = "changé"
    with pytest.raises(HistoryError):
        row.save()
    with pytest.raises(HistoryError):
        row.delete()
    with pytest.raises(HistoryError):
        ItemReopening.objects.filter(pk=row.pk).update(reason_comment="changé")
    with pytest.raises(HistoryError):
        ItemReopening.objects.filter(pk=row.pk).delete()
    assert ItemReopening.objects.get(pk=row.pk).reason_comment == ""


@pytest.mark.django_db
def test_a_decided_proposal_is_never_modified_nor_deleted(team: SimpleNamespace) -> None:
    item: RequiredItem = item_of(team, due_date=dt.date(2027, 1, 15))
    proposal = services.propose_due_date(
        item=item, user=team.student, due_date=dt.date(2027, 3, 1), reason=DueDateReason.OTHER
    )
    services.refuse_due_date(proposal=proposal, user=team.director)
    decided = DueDateProposal.objects.get(pk=proposal.pk)
    decided.status = DueDateProposal.Status.ACCEPTED
    with pytest.raises(HistoryError):
        decided.save()
    with pytest.raises(HistoryError):
        decided.delete()
    with pytest.raises(HistoryError):
        DueDateProposal.objects.filter(pk=proposal.pk).update(status="ACCEPTED")
    with pytest.raises(HistoryError):
        DueDateProposal.objects.filter(pk=proposal.pk).delete()
    assert DueDateProposal.objects.get(pk=proposal.pk).status == "REFUSED"


@pytest.mark.django_db
def test_a_pending_proposal_is_deleted_by_nobody_either(team: SimpleNamespace) -> None:
    item = item_of(team, due_date=dt.date(2027, 1, 15))
    proposal = services.propose_due_date(
        item=item, user=team.student, due_date=dt.date(2027, 3, 1), reason=DueDateReason.OTHER
    )
    with pytest.raises(HistoryError):
        proposal.delete()
