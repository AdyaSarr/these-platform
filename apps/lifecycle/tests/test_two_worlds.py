"""Two worlds on the « Fin de thèse » page (M13a, SR-06). Method:
tests/two_worlds.py (its e-mails include the M13a closure request)."""

from __future__ import annotations

from typing import Any

import pytest

from apps.accounts.roles import Role
from tests import two_worlds as w

pytestmark = pytest.mark.django_db(transaction=True)

MODULE = w.Module(
    "lifecycle",
    None,
    lambda team, shared: [w.url(team, "lifecycle:page")],
    lambda team, shared, role: [(w.url(team, "lifecycle:page"), "<!-- /bell -->")],
)


@pytest.mark.parametrize("role", w.ROLES, ids=w.ROLE_IDS)
def test_a_role_cannot_tell_the_worlds_apart(role: Role, time_machine: Any) -> None:
    w.run(MODULE, role, time_machine)
