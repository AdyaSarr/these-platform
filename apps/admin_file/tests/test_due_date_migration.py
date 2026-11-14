"""Migration 0004: mandatory catalogue items without a due date get the date of
the catalogue AS FROZEN in the migration (2026-11-14), then 0005 forbids none.

Replayed from 0002 on rows written with the historical models; the expected
dates are written out by hand here, never computed by application code.
"""

import datetime as dt
import uuid

import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor

from apps.admin_file.models import RequiredItem

BEFORE = [("admin_file", "0002_reminderlog")]

# ThesisFactory's dates: CIFRE start 2026-03-01, first enrollment 2026-03-15.
EXPECTED = {
    "employment_contract": dt.date(2026, 3, 1),  # CIFRE start + 0
    "cifre_agreement": dt.date(2026, 3, 31),  # CIFRE start + 30
    "collaboration_contract": dt.date(2026, 5, 30),  # CIFRE start + 90
    "funding_proof": dt.date(2026, 3, 15),  # first enrollment + 0
    "fsd_zrr_clearance": dt.date(2026, 3, 1),  # CIFRE start + 0
    "adum_enrollment": dt.date(2026, 3, 15),  # first enrollment + 0
    "doctoral_charter": dt.date(2026, 4, 14),  # first enrollment + 30
    "training_agreement": dt.date(2026, 6, 13),  # first enrollment + 90
    "training_plan": dt.date(2026, 9, 11),  # first enrollment + 180
    "cvec_certificate": dt.date(2026, 3, 15),  # first enrollment + 0
}


@pytest.mark.django_db(transaction=True)
def test_replayed_from_0002_gives_the_frozen_dates() -> None:
    executor = MigrationExecutor(connection)
    executor.migrate(BEFORE)
    state = executor.loader.project_state(BEFORE).apps
    thesis_model = state.get_model("accounts", "Thesis")
    item_model = state.get_model("admin_file", "RequiredItem")
    thesis = thesis_model.objects.create(
        id=uuid.uuid4(),
        title="Thèse fictive",
        cifre_start_date=dt.date(2026, 3, 1),
        first_enrollment_date=dt.date(2026, 3, 15),
        planned_end_date=dt.date(2029, 2, 28),
    )
    common = {"thesis": thesis, "section": "ACADEMIC", "validator_roles": ["DIRECTOR"],
              "default_level": "INTERNAL_LIRMM", "attachment_required": False}  # fmt: skip
    for code in EXPECTED:
        item_model.objects.create(id=uuid.uuid4(), code=code, mandatory=True, due_date=None,
                                  **common)  # fmt: skip
    kept = dt.date(2027, 7, 7)
    item_model.objects.create(id=uuid.uuid4(), code="old_item", mandatory=True, due_date=kept,
                              **common)  # fmt: skip
    item_model.objects.create(id=uuid.uuid4(), code="optional_item", mandatory=False,
                              due_date=None, **common)  # fmt: skip
    item_model.objects.create(id=uuid.uuid4(), code="custom-0123456789ab", label="Fictive",
                              mandatory=True, due_date=None, **common)  # fmt: skip

    executor = MigrationExecutor(connection)
    executor.migrate(executor.loader.graph.leaf_nodes())

    dates = dict(RequiredItem.objects.values_list("code", "due_date"))
    for code, expected in EXPECTED.items():
        assert dates[code] == expected, code
    assert dates["old_item"] == kept  # an existing date is never changed
    assert dates["optional_item"] is None
    assert dates["custom-0123456789ab"] is None  # custom items: the student's rule


def test_the_migration_imports_no_application_code() -> None:
    import ast
    from pathlib import Path

    path = next(Path(__file__).resolve().parents[1].glob("migrations/0004_*.py"))
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.ImportFrom):
            assert not (node.module or "").startswith("apps"), node.module
        if isinstance(node, ast.Import):
            assert not any(a.name.startswith("apps") for a in node.names)
