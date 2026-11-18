"""Two worlds on the thesis dashboard (SR-06): its cards, tiles and « À venir »
agenda, the team page and the CSI file. Method: tests/two_worlds.py."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from apps.accounts.roles import Role
from apps.core.confidentiality import ConfidentialityLevel as L
from tests import two_worlds as w

pytestmark = pytest.mark.django_db(transaction=True)


def _surfaces(team: SimpleNamespace, shared: w.Shared) -> list[str]:
    return [w.url(team, f"accounts:{n}") for n in ("dashboard", "team", "csi_file")]


def _expected(team: SimpleNamespace, shared: w.Shared, role: Role) -> list[tuple[str, str]]:
    if role is Role.CSI_MEMBER:
        return [(w.url(team, "accounts:csi_file"), "<!-- /bell -->")]
    meeting = w.url(team, "meetings:detail", meeting_id=shared.meetings[L.PUBLIC].pk)
    return [(w.url(team, "accounts:dashboard"), meeting)]  # in the « À venir » agenda


MODULE = w.Module("accounts", None, _surfaces, _expected)


@pytest.mark.parametrize("role", w.ROLES, ids=w.ROLE_IDS)
def test_a_role_cannot_tell_the_worlds_apart(role: Role, time_machine: Any) -> None:
    w.run(MODULE, role, time_machine)
