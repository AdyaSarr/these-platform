"""Two worlds (M12): for each restricted role, its .ics is identical byte for byte
when only objects it does not read differ (their existence included)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from django.core.cache import cache

from apps.accounts.permissions import LEVEL_ROLES
from apps.accounts.roles import Role
from apps.core.confidentiality import ConfidentialityLevel as L
from apps.feeds.tests import helpers as h

pytestmark = pytest.mark.django_db(transaction=True)

R = Role
ROLES = [R.CO_SUPERVISOR, R.INDUSTRIAL_CO_SUPERVISOR, R.INDUSTRIAL_TUTOR, R.DIRECTOR]


def _shared(team: SimpleNamespace, user: Any) -> None:
    h.meeting(team, L.PUBLIC, days=3, attendees=[team.student, user])
    for destination, payer, day in (("LIRMM", "LIRMM", 2), ("ROUSSET", "ST", 3),
                                    ("CONFERENCE", "LIRMM", 4)):  # fmt: skip
        h.mission(team, destination, payer, start=h.D(2027, 2, day))
    from apps.conferences.tests.helpers import deadline_of, default_edition

    h.submission(team, L.PUBLIC)
    deadline_of(team, default_edition(team))  # a deadline the role reads
    _approved(h.objective(team, L.PUBLIC), team.director)


def _approved(objective: Any, by: Any) -> None:
    """Approved, without the director (who does not read INTERNAL_ST): a fixture."""
    from apps.objectives.models import Objective

    Objective.objects.filter(pk=objective.pk).update(approved_at=h.NOW, approved_by=by)


def _hidden_deadline(team: SimpleNamespace, level: L, tag: str) -> None:
    """A deadline of an edition that only a submission at ``level`` aims at."""
    from apps.conferences.tests.helpers import (
        deadline_of,
        edition_of,
        submission_of,
        venue_of,
    )

    host = tag.lower().replace("_", "-")
    venue = venue_of(team, acronym=f"HID{tag}"[:20], url=f"https://hid-{host}.example.org/")
    edition = edition_of(team, venue=venue)
    deadline_of(team, edition)
    submission_of(team, edition=edition, level=level, title="TEMOIN-FUITE-TITRE-ARTICLE",
                  st_work=level != L.INTERNAL_LIRMM and level != L.PUBLIC)  # fmt: skip


def _hidden(team: SimpleNamespace, role: Role, world: str) -> None:
    others = [u for r, u in ((R.DIRECTOR, team.director), (R.INDUSTRIAL_TUTOR, team.tutor),
                             (R.CO_SUPERVISOR, team.co_supervisor)) if r is not role]  # fmt: skip
    for level in L:
        if role in LEVEL_ROLES[level]:
            continue
        readers = [u for r, u in ((R.DIRECTOR, team.director), (R.INDUSTRIAL_TUTOR, team.tutor),
                                  (R.CO_SUPERVISOR, team.co_supervisor))
                   if r in LEVEL_ROLES[level] and r is not role]  # fmt: skip
        for n in range(2 if world == "a" else 1):
            h.meeting(team, level, days=5 + n, attendees=[team.student, *readers[:1]])
            h.submission(team, level)  # the shared edition: its deadline is shown anyway
            _hidden_deadline(team, level, f"{world}{level.value}{n}")
            if level is not L.INTERNAL_ST:  # an objective is never INTERNAL_ST (M4)
                _approved(h.objective(team, level), team.director)
    h.meeting(team, L.PUBLIC, days=9, attendees=[team.student, *others[:1]])  # not invited
    if role is R.CO_SUPERVISOR:
        h.mission(team, "ROUSSET", "ST", start=h.D(2027, 3, 1 + (world == "b")))
    if role is R.INDUSTRIAL_CO_SUPERVISOR:
        h.mission(team, "LIRMM", "LIRMM", start=h.D(2027, 3, 1 + (world == "b")))
    if role is not R.DIRECTOR:
        h.item(team, "ACADEMIC", [R.DIRECTOR])


@pytest.mark.parametrize("role", ROLES, ids=lambda r: r.value)
def test_the_feed_of_a_role_cannot_tell_the_worlds_apart(role: Role, time_machine: Any) -> None:
    time_machine.move_to(h.NOW, tick=True)
    team = h.make_team()
    user = getattr(team, h.ROLE_ATTRS[role])
    _shared(team, user)
    _feed, token = h.new_feed(user, team.thesis)

    def ics() -> bytes:
        cache.clear()
        time_machine.move_to(h.NOW, tick=False)  # the same DTSTAMP in every state
        body: bytes = h.fetch(token).content
        time_machine.move_to(h.NOW, tick=True)
        return body

    shared_only = ics()
    _hidden(team, role, "a")
    world_a = ics()
    _hidden(team, role, "b")
    world_b = ics()
    assert b"BEGIN:VEVENT" in shared_only  # not vacuous
    assert shared_only == world_a == world_b
