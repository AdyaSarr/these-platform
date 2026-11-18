"""M13a, static: the open statuses of each module are listed in ONE place,
apps/accounts/statuses.py. No other code names a list of thesis statuses (a copy
that drifted would reopen a frozen module, or keep sending its e-mails)."""

from __future__ import annotations

import ast
from pathlib import Path

from apps.accounts.models import Thesis
from apps.accounts.statuses import MODULE_STATUSES, any_open

ROOT = Path(__file__).resolve().parent.parent
TABLE = ROOT / "apps/accounts/statuses.py"
S = Thesis.Status


def _thesis_statuses(node: ast.AST) -> list[str]:
    """The names SETUP or DEFENDED (thesis statuses only) in a tuple, list or set."""
    if not isinstance(node, ast.Tuple | ast.List | ast.Set):
        return []
    return [
        e.attr
        for e in node.elts
        if isinstance(e, ast.Attribute) and e.attr in ("SETUP", "DEFENDED")
    ]


def test_no_list_of_thesis_statuses_outside_the_table() -> None:
    found = {}
    for path in sorted((ROOT / "apps").rglob("*.py")):
        if "tests" in path.parts or "migrations" in path.parts or path == TABLE:
            continue
        for node in ast.walk(ast.parse(path.read_text(), filename=str(path))):
            if names := _thesis_statuses(node):
                found[f"{path.relative_to(ROOT)}:{node.lineno}"] = names
    assert found == {}


def test_every_module_reads_the_table() -> None:
    users = [
        p.relative_to(ROOT).as_posix()
        for p in sorted((ROOT / "apps").rglob("*.py"))
        if "tests" not in p.parts and "statuses import" in p.read_text()
    ]
    for expected in (
        "apps/admin_file/services.py", "apps/admin_file/reminders.py",
        "apps/admin_file/notifications.py", "apps/objectives/services.py",
        "apps/meetings/services.py", "apps/meetings/reminders.py",
        "apps/meetings/notifications.py", "apps/conferences/services.py",
        "apps/conferences/reminders.py", "apps/conferences/notifications.py",
        "apps/travel/services.py", "apps/travel/reminders.py", "apps/travel/notifications.py",
        "apps/activity/notifications.py", "apps/activity/digests.py", "apps/feeds/services.py",
        "apps/accounts/services.py", "apps/lifecycle/notifications.py",
    ):  # fmt: skip
        assert expected in users, expected


def test_the_table_is_the_sheet() -> None:
    open_, defended = (S.SETUP, S.ACTIVE), (S.SETUP, S.ACTIVE, S.DEFENDED)
    assert dict(MODULE_STATUSES) == {
        "team": open_, "documents": defended, "admin_file": open_, "objectives": open_,
        "meetings": open_, "conferences": defended, "travel": open_,
        "travel_receipts": defended, "feeds": open_, "lifecycle": (S.ACTIVE, S.DEFENDED),
    }  # fmt: skip
    assert any_open() == (S.SETUP, S.ACTIVE, S.DEFENDED)  # never CLOSED


def test_conferences_writes_in_defended_through_the_table() -> None:
    from apps.conferences import services

    assert MODULE_STATUSES["conferences"] == services.WRITABLE_THESIS
