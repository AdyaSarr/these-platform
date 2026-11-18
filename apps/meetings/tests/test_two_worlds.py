"""Two worlds on every GET route of the meetings (M5a, SR-06): the list (upcoming
and past), the register, the new-meeting page, each shared meeting's page, edit,
duplicate and .ics export, the « linked actions » fragment of each shared
objective. A hidden meeting (its page, edit, duplicate, .ics) answers like an
unknown id. Method: tests/two_worlds.py."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from apps.accounts.permissions import Action
from apps.accounts.roles import Role
from apps.core.confidentiality import ConfidentialityLevel as L
from tests import two_worlds as w

pytestmark = pytest.mark.django_db(transaction=True)

MEETING_PAGES = ("detail", "edit", "duplicate", "ics")


def _surfaces(team: SimpleNamespace, shared: w.Shared) -> list[str]:
    listing = w.url(team, "meetings:list")
    out = [listing, f"{listing}?when=past", w.url(team, "meetings:register"),
           w.url(team, "meetings:new")]  # fmt: skip
    for meeting in [*shared.meetings.values(), shared.past_meeting]:
        out += [w.url(team, f"meetings:{name}", meeting_id=meeting.pk) for name in MEETING_PAGES]
    out += [
        w.url(team, "meetings:objective_actions", objective_id=o.pk)
        for o in shared.objectives.values()
    ]
    return out


def _expected(team: SimpleNamespace, shared: w.Shared, role: Role) -> list[tuple[str, str]]:
    listing = w.url(team, "meetings:list")
    return [
        (listing, w.url(team, "meetings:detail", meeting_id=shared.meetings[L.PUBLIC].pk)),
        (f"{listing}?when=past",
         w.url(team, "meetings:detail", meeting_id=shared.past_meeting.pk)),
        (w.url(team, "meetings:register"), "Décision partagée fictive"),
        (w.url(team, "meetings:objective_actions",
               objective_id=shared.objectives[L.PUBLIC].pk), "Action partagée fictive"),
        (w.url(team, "meetings:ics", meeting_id=shared.meetings[L.PUBLIC].pk), "BEGIN:VEVENT"),
    ]  # fmt: skip


def _probes(team: SimpleNamespace, hidden: w.Hidden) -> list[tuple[str, str]]:
    out = [
        (w.url(team, f"meetings:{name}", meeting_id=m.pk),
         w.unknown(team, f"meetings:{name}", "meeting_id"))
        for m in hidden.meetings for name in MEETING_PAGES
    ]  # fmt: skip
    out += [
        (w.url(team, "meetings:objective_actions", objective_id=o.pk),
         w.unknown(team, "meetings:objective_actions", "objective_id"))
        for o in hidden.objectives
    ]  # fmt: skip
    return out


MODULE = w.Module("meetings", Action.MEETING_READ, _surfaces, _expected, _probes)


@pytest.mark.parametrize("role", w.ROLES, ids=w.ROLE_IDS)
def test_a_role_cannot_tell_the_worlds_apart(role: Role, time_machine: Any) -> None:
    w.run(MODULE, role, time_machine)
