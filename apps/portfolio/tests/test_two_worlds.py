"""Two worlds on every GET route of the portfolio (M9a, SR-06): the list of the
trainings (its totals, integrity and mention), the new-training page, each shared
training's page and edit page, the ADUM summary (by year, with and without the
internal trainings) and the settings. A hidden training answers like an unknown
id. Method: tests/two_worlds.py."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from apps.accounts.permissions import Action
from apps.accounts.roles import Role
from apps.core.confidentiality import ConfidentialityLevel as L
from tests import two_worlds as w

pytestmark = pytest.mark.django_db(transaction=True)

TRAINING_PAGES = ("detail", "edit")


def _surfaces(team: SimpleNamespace, shared: w.Shared) -> list[str]:
    adum = w.url(team, "portfolio:adum")
    out = [
        w.url(team, "portfolio:trainings"), w.url(team, "portfolio:new"), adum,
        f"{adum}?year=1", f"{adum}?year=1&internal=1", w.url(team, "portfolio:settings"),
    ]  # fmt: skip
    for t in shared.trainings.values():
        out += [w.url(team, f"portfolio:{n}", training_id=t.pk) for n in TRAINING_PAGES]
    return out


def _expected(team: SimpleNamespace, shared: w.Shared, role: Role) -> list[tuple[str, str]]:
    shown = shared.trainings[L.PUBLIC]
    return [
        (w.url(team, "portfolio:trainings"),
         w.url(team, "portfolio:detail", training_id=shown.pk)),
        (w.url(team, "portfolio:detail", training_id=shown.pk), "Formation partagée fictive"),
    ]  # fmt: skip


def _probes(team: SimpleNamespace, hidden: w.Hidden) -> list[tuple[str, str]]:
    return [
        (w.url(team, f"portfolio:{n}", training_id=t.pk),
         w.unknown(team, f"portfolio:{n}", "training_id"))
        for t in hidden.trainings for n in TRAINING_PAGES
    ]  # fmt: skip


MODULE = w.Module("portfolio", Action.TRAINING_READ, _surfaces, _expected, _probes)


@pytest.mark.parametrize("role", w.ROLES, ids=w.ROLE_IDS)
def test_a_role_cannot_tell_the_worlds_apart(role: Role, time_machine: Any) -> None:
    w.run(MODULE, role, time_machine)
