"""M13a, static: every copy of « the open statuses » says (SETUP, ACTIVE).

The tuple is copied in several modules (services and reminders). A copy that
drifted (DEFENDED added by mistake) would reopen a frozen module. M6 alone keeps
writing in DEFENDED, through WRITABLE_THESIS = OPEN_THESIS + (DEFENDED,).
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OPEN = ("SETUP", "ACTIVE")


def _statuses(node: ast.AST) -> tuple[str, ...] | None:
    """The thesis statuses a tuple or list literal names (Thesis.Status.X), or None."""
    if not isinstance(node, ast.Tuple | ast.List):
        return None
    names = []
    for element in node.elts:
        if not (isinstance(element, ast.Attribute) and isinstance(element.value, ast.Attribute)):
            return None
        if element.value.attr != "Status":
            return None
        names.append(element.attr)
    return tuple(names) or None


def _constants() -> dict[str, tuple[Path, ast.AST]]:
    found: dict[str, tuple[Path, ast.AST]] = {}
    for path in sorted((ROOT / "apps").rglob("*.py")):
        if "tests" in path.parts or "migrations" in path.parts:
            continue
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in tree.body:
            targets = node.targets if isinstance(node, ast.Assign) else []
            for target in targets:
                if isinstance(target, ast.Name) and target.id.isupper():
                    found[f"{path.relative_to(ROOT)}:{target.id}"] = (path, node.value)
    return found


def test_every_open_constant_is_setup_and_active() -> None:
    copies = {
        key: _statuses(value)
        for key, (_path, value) in _constants().items()
        if key.rsplit(":", 1)[1].startswith("OPEN") and _statuses(value) is not None
    }
    assert len(copies) >= 8, copies  # not vacuous: M3, M4, M5a, M6, M7, M11, M12...
    assert {key: names for key, names in copies.items() if names != OPEN} == {}


def test_no_other_constant_lists_open_statuses_under_another_name() -> None:
    """A tuple of statuses under another name is either WRITABLE_THESIS (M6) or
    names no SETUP: no hidden copy of the open statuses."""
    others = {
        key: names
        for key, (_path, value) in _constants().items()
        if not key.rsplit(":", 1)[1].startswith("OPEN")
        and (names := _statuses(value)) is not None
        and "SETUP" in names
    }
    assert others == {}, others


def test_conferences_writes_in_defended_through_one_constant() -> None:
    source = (ROOT / "apps/conferences/services.py").read_text()
    assert "WRITABLE_THESIS = OPEN_THESIS + (Thesis.Status.DEFENDED,)" in source
    from apps.accounts.models import Thesis
    from apps.conferences import services

    assert services.WRITABLE_THESIS == (
        Thesis.Status.SETUP, Thesis.Status.ACTIVE, Thesis.Status.DEFENDED,
    )  # fmt: skip
