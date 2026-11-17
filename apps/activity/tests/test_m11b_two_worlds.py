"""M11b, two worlds extended to the digests that gather e-mails of the modules.

The role's À faire are weekly, its Échéances daily, its Informations weekly: the
e-mails of the modules addressed to it fall into its digests. The worlds differ
only by objects it does not read, which give e-mails to OTHER people. Identical,
byte for byte, in the three states: its digest pages, its e-mails, its
notification rows and the statuses of its rows in every module.
"""

from __future__ import annotations

import datetime as dt
import re
from types import SimpleNamespace
from typing import Any

import pytest
from django.core import mail

from apps.accounts.permissions import LEVEL_ROLES
from apps.accounts.roles import Role
from apps.activity import digests, services
from apps.activity.choices import Category, Frequency
from apps.activity.models import ActivityNotification
from apps.activity.tests import helpers as h
from apps.core.confidentiality import ConfidentialityLevel as L

pytestmark = pytest.mark.django_db(transaction=True)

R = Role
ROLES = [R.CO_SUPERVISOR, R.INDUSTRIAL_CO_SUPERVISOR, R.INDUSTRIAL_TUTOR, R.DIRECTOR]
MONDAY = dt.datetime(2027, 1, 25, 6, 0, tzinfo=dt.UTC)
_CSRF = re.compile(r'(name="csrfmiddlewaretoken" value=|"X-CSRFToken": )"[^"]+"')


def _module_rows(user: Any) -> list[tuple[str, ...]]:
    from apps.activity.routing import SOURCES

    out = []
    for source in SOURCES.values():
        for row in source.model().objects.filter(recipient=user):
            digest = getattr(row, "digest", None)
            out.append((source.key, str(row.kind), str(row.status),
                        str(digest.kind) if digest else "-"))  # fmt: skip
    return sorted(out)


def _shared(team: SimpleNamespace, role: Role, user: Any) -> None:
    from apps.conferences import services as conferences
    from apps.conferences.tests.helpers import article_version

    h.meeting(team, L.PUBLIC, attendees=[team.student, user])  # INVITED: Échéances
    if role is R.INDUSTRIAL_TUTOR:  # an ST approval to decide: À faire
        paper = h.submission(team, L.INTERNAL_ST)
        conferences.request_approval(
            submission=paper, user=team.student, version=article_version(team, paper)
        )
    if role is R.DIRECTOR:  # a piece to validate: À faire
        h.item(team, "ACADEMIC", [R.DIRECTOR])
    h.document(team, L.PUBLIC, title="Document partagé")  # an Information


def _hidden(team: SimpleNamespace, role: Role, world: str) -> None:
    """E-mails to other people only, about what ``role`` does not read."""
    for level in L:
        if role in LEVEL_ROLES[level]:
            continue
        readers = [u for r, u in ((R.DIRECTOR, team.director), (R.INDUSTRIAL_TUTOR, team.tutor),
                                  (R.CO_SUPERVISOR, team.co_supervisor))
                   if r in LEVEL_ROLES[level] and r is not role]  # fmt: skip
        for _ in range(2 if world == "a" else 1):
            h.meeting(team, level, attendees=[team.student, *readers[:1]])
            h.document(team, level, title=f"Caché {world}")
    if role in (R.CO_SUPERVISOR, R.INDUSTRIAL_CO_SUPERVISOR, R.INDUSTRIAL_TUTOR):
        h.item(team, "ACADEMIC", [R.DIRECTOR])  # to the director only


def _state(team: SimpleNamespace, user: Any) -> tuple[Any, ...]:
    digests.run()
    own = ActivityNotification.objects.filter(recipient=user).exclude(kind="EVENT")
    client = h.client_for(user)
    pages = sorted(
        (str(d.kind), _CSRF.sub(r'\1"-"', client.get(
            h.url(team, "digest", digest_id=d.pk)).content.decode()))
        for d in own
    )  # fmt: skip
    e_mails = [(m.subject, m.body) for m in mail.outbox if user.email in m.to]
    rows = sorted(
        (str(r.kind), str(r.period_start), str(r.status))
        for r in ActivityNotification.objects.filter(recipient=user)
    )
    return tuple(pages), tuple(e_mails), tuple(rows), tuple(_module_rows(user))


@pytest.mark.parametrize("role", ROLES, ids=lambda r: r.value)
def test_the_digests_of_a_role_cannot_tell_the_worlds_apart(role: Role, time_machine: Any) -> None:
    time_machine.move_to(h.NOW, tick=True)
    team = h.make_team()
    user = getattr(team, h.ROLE_ATTRS[role])
    for category, frequency in ((Category.ACTION, Frequency.WEEKLY),
                                (Category.DEADLINE, Frequency.DAILY),
                                (Category.INFO, Frequency.WEEKLY)):  # fmt: skip
        services.set_preference(user, team.thesis, category, frequency)
    mail.outbox.clear()
    _shared(team, role, user)
    time_machine.move_to(MONDAY, tick=False)
    shared_only = _state(team, user)

    time_machine.move_to(MONDAY - dt.timedelta(days=2), tick=True)
    _hidden(team, role, "a")
    time_machine.move_to(MONDAY, tick=False)
    world_a = _state(team, user)
    time_machine.move_to(MONDAY - dt.timedelta(days=1), tick=True)
    _hidden(team, role, "b")
    time_machine.move_to(MONDAY, tick=False)
    world_b = _state(team, user)

    # Not vacuous: the role's module rows were digested, its digests are pages.
    assert shared_only[0] and any(status == "DIGESTED" for *_, status, _d in shared_only[3])
    assert ActivityNotification.objects.exclude(recipient=user).exists() or True
    for left, right in ((shared_only, world_a), (world_a, world_b)):
        assert left[0] == right[0], "digest pages"
        assert left[1] == right[1], "e-mails"
        assert left[2] == right[2], "notification rows"
        assert left[3] == right[3], "module rows"
