"""Two worlds on every GET route of the objectives (M4, SR-06): the page of the
year (its list, its statistics and their mention), the new-objective page, each
shared objective's page, edit page and comments. A hidden objective answers like
an unknown id. Method: tests/two_worlds.py."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from apps.accounts.permissions import Action
from apps.accounts.roles import Role
from apps.core.confidentiality import ConfidentialityLevel as L
from tests import two_worlds as w

pytestmark = pytest.mark.django_db(transaction=True)

OBJECTIVE_PAGES = ("detail", "edit", "comments")


def _surfaces(team: SimpleNamespace, shared: w.Shared) -> list[str]:
    dashboard = w.url(team, "objectives:dashboard")
    out = [dashboard, f"{dashboard}?year=1", f"{dashboard}?year=2", w.url(team, "objectives:new")]
    for o in shared.objectives.values():
        out += [w.url(team, f"objectives:{n}", objective_id=o.pk) for n in OBJECTIVE_PAGES]
    return out


def _expected(team: SimpleNamespace, shared: w.Shared, role: Role) -> list[tuple[str, str]]:
    goal = shared.objectives[L.PUBLIC]
    return [
        (w.url(team, "objectives:dashboard"),
         w.url(team, "objectives:detail", objective_id=goal.pk)),
        (w.url(team, "objectives:detail", objective_id=goal.pk), "TEMOIN-FUITE-TITRE-OBJECTIF"),
    ]  # fmt: skip


def _probes(team: SimpleNamespace, hidden: w.Hidden) -> list[tuple[str, str]]:
    return [
        (w.url(team, f"objectives:{n}", objective_id=o.pk),
         w.unknown(team, f"objectives:{n}", "objective_id"))
        for o in hidden.objectives for n in OBJECTIVE_PAGES
    ]  # fmt: skip


MODULE = w.Module("objectives", Action.OBJECTIVE_READ, _surfaces, _expected, _probes)


@pytest.mark.parametrize("role", w.ROLES, ids=w.ROLE_IDS)
def test_a_role_cannot_tell_the_worlds_apart(role: Role, time_machine: Any) -> None:
    w.run(MODULE, role, time_machine)
