"""SR-01 (docs/security/revue-2026-11.md): « on ne voit l'existence que de ce qu'on
peut lire ». One rule for the meetings, objectives and submissions: the module's
read action AND a readable level (permissions.readable_levels, the managers'
readable_by), as the activity feed and the iCal feed. The catalogue of the
conferences is the documented exception.

Also: no signal of a linked document the reader cannot read (point B); a link to
an unreadable objective is kept by the server, never sent to the page (point C);
the objectives' statistics say they are computed on the objectives the reader
can read, without any total (decision of lot B)."""

from __future__ import annotations

import datetime as dt
import re
from types import MappingProxyType, SimpleNamespace
from typing import Any

import pytest
from django.core.exceptions import PermissionDenied
from django.urls import reverse

from apps.accounts.permissions import LEVEL_ROLES, ROLE_ACTIONS, Action
from apps.accounts.roles import Role
from apps.activity.tests import helpers as h
from apps.core.confidentiality import ConfidentialityLevel as L
from apps.vault.models import AuditEvent

pytestmark = pytest.mark.django_db(transaction=True)
R = Role
MENTION = "Calculé sur les objectifs que vous pouvez lire."


@pytest.fixture
def team(time_machine: Any) -> SimpleNamespace:
    time_machine.move_to(h.NOW, tick=True)
    return h.make_team()


def _user(team: SimpleNamespace, role: Role) -> Any:
    return getattr(team, h.ROLE_ATTRS[role])


def _url(team: SimpleNamespace, name: str, **kw: Any) -> str:
    return reverse(name, kwargs={"thesis_id": team.thesis.pk, **kw})


# --------------------------------------------------------------------------- the rule


@pytest.mark.parametrize("role", list(R), ids=lambda r: r.value)
def test_readable_levels_are_those_of_level_roles(team: SimpleNamespace, role: Role) -> None:
    from apps.accounts.permissions import readable_levels

    expected = {level for level in L if role in LEVEL_ROLES[level]}
    assert readable_levels(_user(team, role), team.thesis) == frozenset(expected)
    assert readable_levels(team.outsider, team.thesis) == frozenset()


@pytest.mark.parametrize("role", list(R), ids=lambda r: r.value)
def test_readable_by_is_the_action_and_the_level(team: SimpleNamespace, role: Role) -> None:
    from apps.conferences.models import Submission
    from apps.meetings.models import Meeting
    from apps.objectives.models import OBJECTIVE_LEVELS, Objective

    meetings = {level: h.meeting(team, level) for level in L}
    submissions = {level: h.submission(team, level) for level in L}
    objectives = {level: h.objective(team, level) for level in OBJECTIVE_LEVELS}
    user = _user(team, role)
    for model, objects, action in (
        (Meeting, meetings, Action.MEETING_READ),
        (Submission, submissions, Action.SUBMISSION_READ),
        (Objective, objectives, Action.OBJECTIVE_READ),
    ):
        seen = set(model.objects.readable_by(user, team.thesis).values_list("pk", flat=True))
        expected = {
            o.pk for level, o in objects.items()
            if action in ROLE_ACTIONS[role] and role in LEVEL_ROLES[level]
        }  # fmt: skip
        assert seen == expected, (model.__name__, role)
    assert not Meeting.objects.readable_by(team.outsider, team.thesis).exists()


def test_an_unreadable_meeting_is_refused_as_an_unknown_one(team: SimpleNamespace) -> None:
    from apps.meetings import services as meetings

    hidden = h.meeting(team, L.INTERNAL_ST, attendees=[team.student, team.tutor])
    with pytest.raises(PermissionDenied):
        meetings.meeting_for(team.co_supervisor, team.thesis, hidden.pk, operation="detail")
    with pytest.raises(PermissionDenied):
        meetings.meeting_detail(team.co_supervisor, hidden)
    denied = AuditEvent.objects.filter(action="MEETING_ACCESS_DENIED").order_by("seq").last()
    assert denied is not None and denied.details_json["reason"] == "unreadable_level"
    assert meetings.meeting_for(team.tutor, team.thesis, hidden.pk, operation="detail") == hidden


def test_unreadable_objectives_and_submissions_are_refused(team: SimpleNamespace) -> None:
    from apps.conferences import services as conferences
    from apps.objectives import services as objectives

    goal = h.objective(team, L.INTERNAL_LIRMM)
    article = h.submission(team, L.INTERNAL_LIRMM)
    with pytest.raises(PermissionDenied):
        objectives.objective_for(team.tutor, team.thesis, goal.pk, operation="detail")
    with pytest.raises(PermissionDenied):
        conferences.submission_for(team.tutor, team.thesis, article.pk, operation="detail")
    assert objectives.objective_for(team.director, team.thesis, goal.pk, operation="d") == goal


def test_lists_and_counts_leave_out_unreadable_objects(team: SimpleNamespace) -> None:
    from apps.conferences import services as conferences
    from apps.meetings import services as meetings
    from apps.objectives import services as objectives
    from apps.objectives.models import ThesisYear

    h.meeting(team, L.PUBLIC, attendees=[team.student, team.co_supervisor])
    h.meeting(team, L.INTERNAL_ST, attendees=[team.student, team.tutor])
    h.objective(team, L.PUBLIC)
    h.objective(team, L.INTERNAL_LIRMM)
    h.submission(team, L.PUBLIC)
    h.submission(team, L.INTERNAL_LIRMM)
    year = ThesisYear.objects.get(thesis=team.thesis, number=1)
    assert len(meetings.meeting_list(team.co_supervisor, team.thesis, past=False)) == 1
    assert len(meetings.meeting_list(team.tutor, team.thesis, past=False)) == 1
    assert objectives.stats_for(team.tutor, team.thesis, year).stats.drafts == 1
    assert objectives.stats_for(team.director, team.thesis, year).stats.drafts == 2
    assert len(conferences.submission_list(team.tutor, team.thesis)) == 1
    assert sum(n for _, n in conferences.submission_counts(team.tutor, team.thesis)) == 1
    assert sum(n for _, n in conferences.submission_counts(team.director, team.thesis)) == 2


def test_decisions_and_actions_of_an_unreadable_meeting_are_left_out(
    team: SimpleNamespace, time_machine: Any
) -> None:
    from apps.meetings import services as meetings

    goal = h.objective(team, L.PUBLIC)
    hidden = h.meeting(team, L.INTERNAL_ST, days=-2, attendees=[team.student, team.tutor])
    meetings.add_decision(meeting=hidden, user=team.student, text="Décision cachée fictive")
    meetings.add_action(
        meeting=hidden, user=team.student, title="Action cachée fictive",
        assignee=team.student, due_date=h.D(2027, 1, 18), objective=goal,
    )  # fmt: skip
    assert meetings.decision_register(team.co_supervisor, team.thesis) == []
    assert meetings.actions_for_objective(team.co_supervisor, goal) == []
    assert len(meetings.actions_for_objective(team.tutor, goal)) == 1
    request = SimpleNamespace(user=team.co_supervisor)
    block = meetings.dashboard_block(request, team.thesis)
    assert block["overdue_count"] == 0 and block["next_meeting"] is None


# --------------------------------------------------------------------------- M7 links


def test_a_mission_never_shows_nor_takes_an_unreadable_meeting(team: SimpleNamespace) -> None:
    from apps.travel import services as travel

    hidden = h.meeting(team, L.INTERNAL_ST, attendees=[team.student, team.tutor])
    assert travel.link_for(team.co_supervisor, team.thesis, "meeting", str(hidden.pk)) is None
    assert travel.link_for(team.tutor, team.thesis, "meeting", str(hidden.pk)) == hidden
    mission = h.mission(team, "LIRMM", "LIRMM", start=h.D(2027, 2, 3))
    type(mission).objects.filter(pk=mission.pk).update(meeting=hidden)
    page = h.client_for(team.co_supervisor).get(_url(team, "travel:mission", mission_id=mission.pk))
    assert page.status_code == 200
    assert str(hidden.pk) not in page.content.decode()
    assert "Réunion :" not in page.content.decode()  # « Réunion : <type>, <date> »


# --------------------------------------------------------------------------- point B


def test_no_signal_of_an_unreadable_linked_document(team: SimpleNamespace) -> None:
    from apps.conferences import services as conferences
    from apps.meetings import services as meetings
    from apps.objectives import services as objectives

    secret = h.document(team, L.CONFIDENTIAL, title="TEMOIN-FUITE-DOCUMENT-LIE")
    meeting = h.meeting(team, L.PUBLIC, days=-1, attendees=[team.student, team.co_supervisor])
    meetings.link_document(meeting=meeting, user=team.student, document=secret)
    article = h.submission(team, L.PUBLIC)
    conferences.link_document(submission=article, user=team.student, document=secret)
    goal = h.objective(team, L.PUBLIC)
    objectives.add_progress(objective=goal, user=team.student, date=h.D(2027, 1, 19),
                            progress=20, status="IN_PROGRESS", evidence=secret)  # fmt: skip
    client = h.client_for(team.co_supervisor)
    for target in (
        _url(team, "meetings:detail", meeting_id=meeting.pk),
        _url(team, "conferences:submission", submission_id=article.pk),
        _url(team, "objectives:detail", objective_id=goal.pk),
    ):
        html = client.get(target).content.decode()
        assert "TEMOIN-FUITE-DOCUMENT-LIE" not in html, target
        assert "que vous ne pouvez pas lire" not in html, target  # « N document(s) lié(s) »
        assert "vous ne pouvez pas la lire" not in html, target  # « Une preuve est jointe »
        assert str(secret.pk) not in html, target


def test_an_st_approval_is_shown_without_the_name_of_an_unreadable_document(
    team: SimpleNamespace,
) -> None:
    from apps.conferences import services as conferences
    from apps.conferences.tests.helpers import article_version

    article = h.submission(team, L.PUBLIC)
    version = article_version(team, article)  # a CONFIDENTIAL document
    conferences.request_approval(submission=article, user=team.student, version=version)
    html = (
        h.client_for(team.co_supervisor)
        .get(_url(team, "conferences:submission", submission_id=article.pk))
        .content.decode()
    )
    assert "Article fictif" not in html and "Document restreint" not in html
    assert " v1 ·" in html  # the approval is shown, without the document's name
    tutor = (
        h.client_for(team.tutor)
        .get(_url(team, "conferences:submission", submission_id=article.pk))
        .content.decode()
    )
    assert "Article fictif" in tutor


# --------------------------------------------------------------------------- point C


@pytest.fixture
def st_co_supervisor_writes_submissions(monkeypatch: pytest.MonkeyPatch) -> None:
    """Only the student writes submissions today, and the student reads every level:
    the case of point C needs a writer who does not read the objective, so the
    test grants SUBMISSION_WRITE to the company co-supervisor (test only)."""
    from apps.accounts import permissions

    actions = dict(permissions.ROLE_ACTIONS)
    actions[R.INDUSTRIAL_CO_SUPERVISOR] = actions[R.INDUSTRIAL_CO_SUPERVISOR] | {
        Action.SUBMISSION_WRITE
    }
    monkeypatch.setattr(permissions, "ROLE_ACTIONS", MappingProxyType(actions))


@pytest.mark.usefixtures("st_co_supervisor_writes_submissions")
def test_a_hidden_objective_link_is_kept_by_the_server_only(team: SimpleNamespace) -> None:
    from apps.conferences.models import Submission
    from apps.conferences.tests.helpers import submission_of

    goal = h.objective(team, L.INTERNAL_LIRMM)  # not read by the company co-supervisor
    article = submission_of(team, level=L.PUBLIC, title="Article fictif lié", objective=goal)
    client = h.browser_for(team.st_co_supervisor)
    edit = _url(team, "conferences:edit_submission", submission_id=article.pk)
    page = client.get(edit)
    assert page.status_code == 200
    html = page.content.decode()
    assert str(goal.pk) not in html
    fields = dict(re.findall(r'<input[^>]*name="([^"]+)"[^>]*value="([^"]*)"', html))
    fields.pop("csrfmiddlewaretoken", None)
    target = re.search(r'<option value="([^"]+)" selected', html)
    data = {**fields, "title": "Article fictif lié, revu", "objective": "",
            "target": target.group(1) if target else f"edition:{article.edition_id}"}  # fmt: skip
    response = h.browser_post(client, edit, edit, data)
    assert response.status_code in (302, 303), response.content.decode()[:500]
    assert Submission.objects.get(pk=article.pk).objective_id == goal.pk


@pytest.mark.usefixtures("st_co_supervisor_writes_submissions")
def test_a_posted_hidden_objective_id_is_refused_as_an_unknown_one(
    team: SimpleNamespace,
) -> None:
    from apps.conferences.tests.helpers import submission_of

    goal = h.objective(team, L.INTERNAL_LIRMM)
    article = submission_of(team, level=L.PUBLIC, title="Article fictif")
    client = h.browser_for(team.st_co_supervisor)
    edit = _url(team, "conferences:edit_submission", submission_id=article.pk)
    base = {"title": "Article fictif", "target": f"edition:{article.edition_id}"}
    hidden = h.browser_post(client, edit, edit, {**base, "objective": str(goal.pk)})
    absent = "00000000-0000-4000-8000-000000000000"
    unknown = h.browser_post(client, edit, edit, {**base, "objective": absent})
    assert hidden.status_code == unknown.status_code == 400
    strip = lambda r: re.sub(r'value="[^"]*"', "", r.content.decode())  # noqa: E731
    assert strip(hidden) == strip(unknown)


def test_the_choices_offer_readable_objectives_only(team: SimpleNamespace) -> None:
    from apps.meetings.views import _objectives as meeting_choices

    readable = h.objective(team, L.PUBLIC)
    hidden = h.objective(team, L.INTERNAL_LIRMM)
    ids = {pk for pk, _label in meeting_choices(team.tutor, team.thesis)}
    assert ids == {str(readable.pk)} and str(hidden.pk) not in ids


# --------------------------------------------------------------------------- the mention


@pytest.mark.parametrize(
    ("role", "shown"),
    [(R.PHD_STUDENT, False), (R.DIRECTOR, False), (R.INDUSTRIAL_TUTOR, True),
     (R.CO_SUPERVISOR, True), (R.INDUSTRIAL_CO_SUPERVISOR, True), (R.CSI_MEMBER, True)],
    ids=lambda v: getattr(v, "value", str(v)),
)  # fmt: skip
def test_the_statistics_say_they_count_what_the_reader_reads(
    team: SimpleNamespace, role: Role, shown: bool
) -> None:
    """A reader who does not read every level an objective may have: the mention,
    and never a total nor a number of hidden objectives."""
    h.objective(team, L.PUBLIC)
    client = h.client_for(_user(team, role))
    page = client.get(_url(team, "objectives:dashboard")).content.decode()
    assert (MENTION in page) is shown
    home = "accounts:csi_file" if role is R.CSI_MEMBER else "accounts:dashboard"
    tile = client.get(_url(team, home)).content.decode()
    if role is not R.CSI_MEMBER:
        assert (MENTION in tile) is shown
    for html in (page, tile):
        assert "objectifs cachés" not in html and "au total" not in html


def test_the_mention_depends_on_the_role_not_on_the_objectives(
    team: SimpleNamespace, time_machine: Any
) -> None:
    client = h.client_for(team.tutor)
    time_machine.move_to(h.NOW + dt.timedelta(hours=1), tick=False)
    before = client.get(_url(team, "objectives:dashboard")).content.decode()
    assert MENTION in before
    h.objective(team, L.INTERNAL_LIRMM)  # hidden from the tutor
    after = client.get(_url(team, "objectives:dashboard")).content.decode()
    assert before == after
