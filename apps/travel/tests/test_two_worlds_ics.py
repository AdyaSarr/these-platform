"""Two worlds on the .ics exports of the missions (M7, SR-06), for every role: the
export of all missions and each shared mission's. A hidden mission's export
answers like an unknown id. The other travel pages have their own test
(test_minimisation.py). Method: tests/two_worlds.py."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from apps.accounts.permissions import Action
from apps.accounts.roles import Role
from tests import two_worlds as w

pytestmark = pytest.mark.django_db(transaction=True)


def _surfaces(team: SimpleNamespace, shared: w.Shared) -> list[str]:
    out = [w.url(team, "travel:missions_ics")]
    out += [w.url(team, "travel:mission_ics", mission_id=m.pk) for m in shared.missions]
    return out


def _expected(team: SimpleNamespace, shared: w.Shared, role: Role) -> list[tuple[str, str]]:
    return [(w.url(team, "travel:missions_ics"), "BEGIN:VCALENDAR")]


def _probes(team: SimpleNamespace, hidden: w.Hidden) -> list[tuple[str, str]]:
    return [
        (w.url(team, "travel:mission_ics", mission_id=m.pk),
         w.unknown(team, "travel:mission_ics", "mission_id"))
        for m in hidden.missions
    ]  # fmt: skip


MODULE = w.Module("travel", Action.TRAVEL_READ, _surfaces, _expected, _probes,
                  only=frozenset({"missions_ics", "mission_ics"}))  # fmt: skip


@pytest.mark.parametrize("role", w.ROLES, ids=w.ROLE_IDS)
def test_a_role_cannot_tell_the_worlds_apart(role: Role, time_machine: Any) -> None:
    w.run(MODULE, role, time_machine)
