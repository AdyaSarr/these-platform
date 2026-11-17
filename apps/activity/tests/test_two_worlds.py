"""Two worlds (M11a, acceptance criterion 2): for a role, worlds that differ only by
objects the role does not read — their EXISTENCE included, for meetings,
submissions and objectives too (option B) — must look the same, byte for byte:
the Activity page (every filter, every page), the bell and its count, the number
and the bytes of the e-mails (immediate and digests), the notification rows of the
role. Exposed ids are random UUIDs: those shown are the same rows in every world.

Three states are compared: the shared objects only (everything marked read by the
role), then world "a" (hidden objects added), then world "b" (more, of other
kinds). A digest triggered by an unreadable element, a count, a cut before the
filter, an id that leaks an order: each one makes a state differ.
"""

from __future__ import annotations

import datetime as dt
import re
from types import SimpleNamespace
from typing import Any

import pytest
from django.core import mail
from django.urls import reverse

from apps.accounts.roles import Role
from apps.activity import digests, services
from apps.activity.choices import Category, Frequency
from apps.activity.models import ActivityNotification, Event
from apps.activity.tests import helpers as h
from apps.core.confidentiality import ConfidentialityLevel as L

pytestmark = pytest.mark.django_db(transaction=True)

R = Role
ROLES = [R.CO_SUPERVISOR, R.INDUSTRIAL_CO_SUPERVISOR, R.INDUSTRIAL_TUTOR, R.DIRECTOR,
         R.CSI_MEMBER]  # fmt: skip
MONDAY = dt.datetime(2027, 1, 25, 6, 0, tzinfo=dt.UTC)  # 07:00 in Paris
MODULES = ["", "documents", "admin_file", "objectives", "meetings", "conferences", "travel",
           "team"]  # fmt: skip
_CSRF = re.compile(r'(name="csrfmiddlewaretoken" value=)"[^"]+"')
_BELL = re.compile(r'<div class="bell"[^>]*>.*?</div>\s*<!-- /bell -->', re.S)


def _hidden_levels(role: Role, action: Any) -> list[L]:
    from apps.accounts.permissions import LEVEL_ROLES, ROLE_ACTIONS

    if action not in ROLE_ACTIONS[role]:
        return list(L)
    return [level for level in L if role not in LEVEL_ROLES[level]]


def _hidden_destinations(role: Role) -> list[tuple[str, str]]:
    return {
        R.CO_SUPERVISOR: [("ROUSSET", "ST"), ("CONFERENCE", "LIRMM")],
        R.INDUSTRIAL_CO_SUPERVISOR: [("LIRMM", "LIRMM"), ("CONFERENCE", "LIRMM")],
        R.CSI_MEMBER: [("LIRMM", "LIRMM"), ("ROUSSET", "ST")],
    }.get(role, [])


def _hidden_sections(role: Role) -> list[str]:
    return {
        R.INDUSTRIAL_TUTOR: ["ACADEMIC"],
        R.CO_SUPERVISOR: ["CIFRE", "ACADEMIC"],
        R.INDUSTRIAL_CO_SUPERVISOR: ["CIFRE", "ACADEMIC"],
        R.CSI_MEMBER: ["CIFRE", "ACADEMIC"],
    }.get(role, [])


def _shared(team: SimpleNamespace) -> None:
    """Objects of every kind; some readable by the role, some not: the same in all
    three states."""
    from apps.objectives import services as objectives

    for level in L:
        h.document(team, level, title=f"Document partagé {level}")
        h.meeting(team, level, days=3)
        h.submission(team, level)
    goal = h.objective(team, L.PUBLIC)
    h.objective(team, L.INTERNAL_LIRMM)
    h.objective(team, L.INTERNAL_ST)
    objectives.add_comment(objective=goal, user=team.director, body="Avis partagé fictif")
    for day, (destination, payer) in enumerate(
        [("LIRMM", "LIRMM"), ("ROUSSET", "ST"), ("CONFERENCE", "LIRMM")], start=2
    ):
        h.mission(team, destination, payer, start=h.D(2027, 2, day))
    h.item(team, "CIFRE", [R.INDUSTRIAL_TUTOR])


def _hidden(team: SimpleNamespace, role: Role, world: str) -> None:
    """Only what ``role`` does not read; world "b" adds more, of other kinds."""
    from apps.accounts.permissions import Action
    from apps.objectives import services as objectives
    from apps.objectives.models import Objective

    copies = 10 if world == "a" else 3
    for n in range(copies):
        for level in _hidden_levels(role, Action.DOCUMENT_READ):
            h.document(team, level, title=f"Caché {world}{n} {level}")
    for level in _hidden_levels(role, Action.MEETING_READ):
        h.meeting(team, level, days=4 if world == "a" else 5)
    if world == "b":
        for level in _hidden_levels(role, Action.SUBMISSION_READ):
            h.submission(team, level)
        for level in _hidden_levels(role, Action.OBJECTIVE_READ):
            h.objective(team, level)
        if role is R.CSI_MEMBER:  # a comment: the CSI member never reads them
            public = Objective.objects.filter(thesis=team.thesis, level=L.PUBLIC).first()
            objectives.add_comment(objective=public, user=team.director, body="Avis caché")
    for day, (destination, payer) in enumerate(_hidden_destinations(role), start=15):
        h.mission(team, destination, payer, start=h.D(2027, 2, day + (world == "b") * 5))
    for section in _hidden_sections(role):
        h.item(team, section, [R.DIRECTOR])


def _surfaces(team: SimpleNamespace, role: Role) -> list[str]:
    feed = h.url(team, "feed")
    out = [f"{feed}?module={m}" if m else feed for m in MODULES]
    out += [f"{feed}?page=2", f"{feed}?page=3"]
    home = "accounts:csi_file" if role is R.CSI_MEMBER else "accounts:dashboard"
    out.append(reverse(home, kwargs={"thesis_id": team.thesis.pk}))
    out.append(h.url(team, "preferences"))
    return out


def _state(team: SimpleNamespace, role: Role, user: Any) -> tuple[Any, ...]:
    client = h.client_for(user)
    pages = []
    for target in _surfaces(team, role):
        response = client.get(target)
        body = _CSRF.sub(r'\1"-"', response.content.decode())
        if "activity/" not in target:  # another module's page: its bell only
            match = _BELL.search(body)
            body = match.group(0) if match else ""
        pages.append((target, response.status_code, body))
    digests.run()
    e_mails = [(m.subject, m.body) for m in mail.outbox if user.email in m.to]
    rows = sorted(
        (r.kind, str(r.event_id), str(r.period_start), r.status, r.cycle)
        for r in ActivityNotification.objects.filter(recipient=user)
    )
    return tuple(pages), tuple(e_mails), tuple(rows)


@pytest.mark.parametrize("frequency", [Frequency.IMMEDIATE, Frequency.WEEKLY])
@pytest.mark.parametrize("role", ROLES, ids=lambda r: r.value)
def test_a_role_cannot_tell_the_worlds_apart(role: Role, frequency: str, time_machine: Any) -> None:
    time_machine.move_to(h.NOW, tick=True)
    team = h.make_team()
    user = getattr(team, h.ROLE_ATTRS[role])
    services.set_preference(user, team.thesis, Category.INFO, frequency)
    mail.outbox.clear()
    _shared(team)
    services.mark_all_read(user, team.thesis)

    time_machine.move_to(MONDAY, tick=False)
    shared_only = _state(team, role, user)
    events_before = Event.objects.count()

    time_machine.move_to(MONDAY - dt.timedelta(hours=2), tick=True)
    _hidden(team, role, "a")
    time_machine.move_to(MONDAY, tick=False)
    world_a = _state(team, role, user)

    time_machine.move_to(MONDAY - dt.timedelta(hours=1), tick=True)
    _hidden(team, role, "b")
    time_machine.move_to(MONDAY, tick=False)
    world_b = _state(team, role, user)

    # Not vacuous: the worlds hold more events; the role's feed shows elements.
    assert Event.objects.count() > events_before
    feed = dict((t, b) for t, _, b in shared_only[0])[h.url(team, "feed")]
    assert 'class="activity-item"' in feed
    if frequency == Frequency.IMMEDIATE:
        assert shared_only[1]  # the shared events were e-mailed to the role

    for left, right in ((shared_only, world_a), (world_a, world_b)):
        for (target, status_l, body_l), (_, status_r, body_r) in zip(left[0], right[0],
                                                                     strict=True):  # fmt: skip
            assert (status_l, body_l) == (status_r, body_r), target
        assert left[1] == right[1], "e-mails"
        assert left[2] == right[2], "notification rows"
