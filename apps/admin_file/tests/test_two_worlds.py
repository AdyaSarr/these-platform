"""Two worlds on every GET route of the administrative file (M3, SR-06): the file,
the new-item page, each shared item's page. A hidden item (a section the role does
not read) answers like an unknown id. Method: tests/two_worlds.py."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from apps.accounts.permissions import ROLE_ACTIONS
from apps.accounts.roles import Role
from tests import two_worlds as w

pytestmark = pytest.mark.django_db(transaction=True)


def _surfaces(team: SimpleNamespace, shared: w.Shared) -> list[str]:
    out = [w.url(team, "admin_file:file"), w.url(team, "admin_file:new_item")]
    out += [w.url(team, "admin_file:item", item_id=i.pk) for i in shared.items]
    return out


def _expected(team: SimpleNamespace, shared: w.Shared, role: Role) -> list[tuple[str, str]]:
    from apps.admin_file.models import SECTION_READ_ACTION, Section

    read = [
        i for i in shared.items if SECTION_READ_ACTION[Section(i.section)] in ROLE_ACTIONS[role]
    ]
    item_url = lambda i: w.url(team, "admin_file:item", item_id=i.pk)  # noqa: E731
    return [(w.url(team, "admin_file:file"), item_url(i)) for i in read] + [
        (item_url(i), "Pièce fictive") for i in read
    ]


def _probes(team: SimpleNamespace, hidden: w.Hidden) -> list[tuple[str, str]]:
    return [
        (
            w.url(team, "admin_file:item", item_id=i.pk),
            w.unknown(team, "admin_file:item", "item_id"),
        )
        for i in hidden.items
    ]


MODULE = w.Module("admin_file", None, _surfaces, _expected, _probes)


@pytest.mark.parametrize("role", w.ROLES, ids=w.ROLE_IDS)
def test_a_role_cannot_tell_the_worlds_apart(role: Role, time_machine: Any) -> None:
    w.run(MODULE, role, time_machine)
