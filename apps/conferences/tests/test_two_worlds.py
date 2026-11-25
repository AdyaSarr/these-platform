"""Two worlds on every GET route of the conferences (M6, SR-06): deadlines,
policy, the venue and edition pages of the shared catalogue (the documented
exception: shared by the team), the list of submissions, the new-submission page,
each shared submission's page and edit page, the « submissions » fragment of each
shared objective; M9b: a seminar without edition in the catalogue, hidden
posters aimed at it, the link of a submission to its publication details. A
hidden submission answers like an unknown id. Method: tests/two_worlds.py."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from apps.accounts.permissions import Action
from apps.accounts.roles import Role
from apps.core.confidentiality import ConfidentialityLevel as L
from tests import two_worlds as w

pytestmark = pytest.mark.django_db(transaction=True)

SUBMISSION_PAGES = ("submission", "edit_submission")


def _surfaces(team: SimpleNamespace, shared: w.Shared) -> list[str]:
    names = ("deadlines", "policy", "new_venue", "submissions", "new_submission")
    out = [w.url(team, f"conferences:{n}") for n in names]
    out.append(w.url(team, "conferences:venue", venue_id=shared.venue.pk))
    out.append(w.url(team, "conferences:venue", venue_id=shared.seminar.pk))
    out.append(w.url(team, "conferences:edition", edition_id=shared.edition.pk))
    for s in shared.submissions.values():
        out += [w.url(team, f"conferences:{n}", submission_id=s.pk) for n in SUBMISSION_PAGES]
    out += [
        w.url(team, "conferences:objective_submissions", objective_id=o.pk)
        for o in shared.objectives.values()
    ]
    return out


def _expected(team: SimpleNamespace, shared: w.Shared, role: Role) -> list[tuple[str, str]]:
    public = shared.submissions[L.PUBLIC]
    return [
        (w.url(team, "conferences:submissions"),
         w.url(team, "conferences:submission", submission_id=public.pk)),
        (w.url(team, "conferences:objective_submissions",
               objective_id=shared.objectives[L.PUBLIC].pk), "Article partagé fictif PUBLIC"),
        # M9b: the form of the communication, the link to its publication details, and
        # the seminar of the shared catalogue (hidden posters are aimed at it).
        (w.url(team, "conferences:submission", submission_id=public.pk),
         w.url(team, "portfolio:publication", submission_id=public.pk)),
        (w.url(team, "conferences:venue", venue_id=shared.seminar.pk), "Séminaire fictif"),
    ]  # fmt: skip


def _probes(team: SimpleNamespace, hidden: w.Hidden) -> list[tuple[str, str]]:
    out = [
        (w.url(team, f"conferences:{n}", submission_id=s.pk),
         w.unknown(team, f"conferences:{n}", "submission_id"))
        for s in hidden.submissions for n in SUBMISSION_PAGES
    ]  # fmt: skip
    out += [
        (w.url(team, "conferences:objective_submissions", objective_id=o.pk),
         w.unknown(team, "conferences:objective_submissions", "objective_id"))
        for o in hidden.objectives
    ]  # fmt: skip
    return out


MODULE = w.Module("conferences", Action.SUBMISSION_READ, _surfaces, _expected, _probes)


@pytest.mark.parametrize("role", w.ROLES, ids=w.ROLE_IDS)
def test_a_role_cannot_tell_the_worlds_apart(role: Role, time_machine: Any) -> None:
    w.run(MODULE, role, time_machine)
