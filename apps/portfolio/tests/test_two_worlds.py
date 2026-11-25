"""Two worlds on every GET route of the portfolio (M9a, M9b, SR-06): the trainings
(list with totals, integrity and mention, new, detail, edit), the publication
details (list, detail, edit), teaching and outreach (list, new, detail, edit),
the student's skills and professional project, the ADUM summary (by year, with
and without the internal and the accepted items) and the settings. A hidden
training, submission, teaching, outreach action or skill answers like an
unknown id. Method: tests/two_worlds.py.

One module, several read actions: the positive control of each part is checked
for the roles holding its action (``_expected`` reads the role's actions)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from apps.accounts.permissions import ROLE_ACTIONS, Action
from apps.accounts.roles import Role
from apps.core.confidentiality import ConfidentialityLevel as L
from tests import two_worlds as w

pytestmark = pytest.mark.django_db(transaction=True)

PAGES = {  # url name prefix -> (detail, edit) pages and their id
    "training": (("detail", "edit"), "training_id"),
    "publication": (("publication", "edit_publication"), "submission_id"),
    "teaching": (("teaching", "edit_teaching"), "teaching_id"),
    "outreach": (("outreach", "edit_outreach"), "outreach_id"),
}


def _surfaces(team: SimpleNamespace, shared: w.Shared) -> list[str]:
    adum = w.url(team, "portfolio:adum")
    lists = ("trainings", "new", "settings", "publications", "teachings", "new_teaching",
             "outreach_list", "new_outreach", "skills", "new_skill", "career")  # fmt: skip
    out = [w.url(team, f"portfolio:{n}") for n in lists]
    out += [adum, f"{adum}?year=1", f"{adum}?year=1&internal=1",
            f"{adum}?year=1&internal=1&accepted=1"]  # fmt: skip
    objects = {"training": shared.trainings, "publication": shared.submissions,
               "teaching": shared.teachings, "outreach": shared.outreach}  # fmt: skip
    for key, rows in objects.items():
        names, kwarg = PAGES[key]
        for obj in rows.values():
            out += [w.url(team, f"portfolio:{n}", **{kwarg: obj.pk}) for n in names]
    out.append(w.url(team, "portfolio:edit_skill", skill_id=shared.skill.pk))
    return out


def _expected(team: SimpleNamespace, shared: w.Shared, role: Role) -> list[tuple[str, str]]:
    actions = ROLE_ACTIONS[role]
    out: list[tuple[str, str]] = []
    if Action.TRAINING_READ in actions:
        shown = shared.trainings[L.PUBLIC]
        out += [
            (w.url(team, "portfolio:trainings"),
             w.url(team, "portfolio:detail", training_id=shown.pk)),
            (w.url(team, "portfolio:detail", training_id=shown.pk), "Formation partagée fictive"),
        ]  # fmt: skip
    if Action.SUBMISSION_READ in actions:
        public = shared.submissions[L.PUBLIC]
        out += [
            (w.url(team, "portfolio:publications"),
             w.url(team, "portfolio:publication", submission_id=public.pk)),
            (w.url(team, "portfolio:publication", submission_id=public.pk),
             "10.12345/partage.public"),
        ]  # fmt: skip
    if Action.TEACHING_READ in actions:
        course = shared.teachings[L.PUBLIC]
        out += [
            (w.url(team, "portfolio:teachings"),
             w.url(team, "portfolio:teaching", teaching_id=course.pk)),
            (w.url(team, "portfolio:teaching", teaching_id=course.pk), "Cours partagé fictif"),
        ]  # fmt: skip
    if Action.OUTREACH_READ in actions:
        action = shared.outreach[L.PUBLIC]
        out += [
            (w.url(team, "portfolio:outreach_list"),
             w.url(team, "portfolio:outreach", outreach_id=action.pk)),
            (w.url(team, "portfolio:outreach", outreach_id=action.pk), "Action partagée fictive"),
        ]  # fmt: skip
    assert out or role is Role.GUEST, role  # every role of w.ROLES reads a part
    return out


def _probes(team: SimpleNamespace, hidden: w.Hidden) -> list[tuple[str, str]]:
    objects = {"training": hidden.trainings, "publication": hidden.submissions,
               "teaching": hidden.teachings, "outreach": hidden.outreach}  # fmt: skip
    out = []
    for key, rows in objects.items():
        names, kwarg = PAGES[key]
        out += [
            (w.url(team, f"portfolio:{n}", **{kwarg: obj.pk}),
             w.unknown(team, f"portfolio:{n}", kwarg))
            for obj in rows for n in names
        ]  # fmt: skip
    out += [
        (w.url(team, "portfolio:edit_skill", skill_id=s.pk),
         w.unknown(team, "portfolio:edit_skill", "skill_id"))
        for s in hidden.skills
    ]  # fmt: skip
    return out


MODULE = w.Module("portfolio", None, _surfaces, _expected, _probes)


@pytest.mark.parametrize("role", w.ROLES, ids=w.ROLE_IDS)
def test_a_role_cannot_tell_the_worlds_apart(role: Role, time_machine: Any) -> None:
    w.run(MODULE, role, time_machine)
