"""The « À traiter » tile counts what falls to the viewer only: their overdue
actions (the Meetings card lists them), the approvals they must decide (the
"To decide" queue of travel, the tutor's ST approvals), their missing receipts.

Before: the tile added up every overdue action of the thesis and the overdue
objectives, so a co-supervisor read « 2 actions en retard » in the tile and « Aucune
de vos actions n'est en retard » in the card.
"""

from __future__ import annotations

import datetime as dt
import html as html_module
import re
from types import SimpleNamespace
from typing import Any

import pytest
from django.urls import reverse

from apps.travel.tests.helpers import D, client_for, make_team, mission_of

pytestmark = pytest.mark.django_db(transaction=True)

LATER = dt.datetime(2027, 2, 15, 9, 0, tzinfo=dt.UTC)
EXPECTED = {"student": 2, "director": 2, "co_supervisor": 0, "tutor": 2}


@pytest.fixture
def team(time_machine: Any) -> SimpleNamespace:
    """Overdue actions: 1 of the student, 2 of the director, none of the
    co-supervisor; a mission approval and an ST approval for the tutor; a done
    mission of the student without receipt."""
    from apps.conferences import services as conferences
    from apps.conferences.tests.helpers import article_version, submission_of
    from apps.meetings import services as meetings
    from apps.meetings.tests.helpers import meeting_of
    from apps.travel import services as travel

    time_machine.move_to(dt.datetime(2027, 1, 5, 9, 0, tzinfo=dt.UTC), tick=False)
    team = make_team()
    held = meeting_of(team)  # 12 January
    time_machine.move_to(dt.datetime(2027, 1, 13, 9, 0, tzinfo=dt.UTC), tick=False)
    for assignee in (team.student, team.director, team.director):
        meetings.add_action(
            meeting=held,
            user=team.student,
            title="Action fictive",
            assignee=assignee,
            due_date=D(2027, 1, 15),
        )
    time_machine.move_to(dt.datetime(2027, 1, 20, 9, 0, tzinfo=dt.UTC), tick=False)
    ordered = mission_of(team, payer="ST", start_date=D(2027, 3, 8), end_date=D(2027, 3, 9))
    travel.request_order(mission=ordered, user=team.student, estimated_total="1")
    submission = submission_of(team)
    conferences.request_approval(
        submission=submission, user=team.student, version=article_version(team, submission)
    )
    done = mission_of(team, payer="OTHER")  # 8 to 10 February
    time_machine.move_to(LATER, tick=False)
    travel.mark_done(mission=done, user=team.student)
    travel.add_expense(mission=done, user=team.student, category="TRANSPORT", actual="10")
    return team


def _page(team: SimpleNamespace, role: str) -> str:
    response = client_for(team, role).get(
        reverse("accounts:dashboard", kwargs={"thesis_id": team.thesis.pk})
    )
    assert response.status_code == 200
    return html_module.unescape(response.content.decode())


def _tile_total(page: str) -> int:
    tile = re.search(r'data-tile="alerts">.*?<span class="tile-value">(\d+)</span>', page, re.S)
    assert tile is not None
    return int(tile.group(1))


def _mine_elsewhere(team: SimpleNamespace, role: str, page: str) -> int:
    """What the dashboard and the module pages show as the viewer's own."""
    from apps.accounts.permissions import Action, can
    from apps.conferences import services as conferences
    from apps.travel import services as travel
    from apps.travel.models import Mission

    user = getattr(team, role)
    card = re.search(r'data-block="meetings">(.*?)</section>', page, re.S)
    assert card is not None
    actions = len(re.findall(r"<li>", card.group(1)))  # « my overdue actions »
    queue = client_for(team, role).get(
        reverse("travel:orders", kwargs={"thesis_id": team.thesis.pk})
    )
    orders = (
        len(re.findall(r"<tr>", queue.content.decode().split("<tbody>", 1)[-1]))
        if queue.status_code == 200 and "<tbody>" in queue.content.decode()
        else 0
    )
    approvals = len(conferences.pending_approvals(user, team.thesis))
    receipts = (
        sum(
            1
            for m in Mission.objects.filter(thesis=team.thesis, status="DONE")
            if travel.expenses_incomplete(m)
        )
        if can(user, Action.TRAVEL_WRITE, team.thesis)
        else 0
    )
    return actions + orders + approvals + receipts


@pytest.mark.parametrize("role", list(EXPECTED))
def test_the_tile_counts_what_falls_to_the_viewer(team: SimpleNamespace, role: str) -> None:
    page = _page(team, role)
    assert _tile_total(page) == _mine_elsewhere(team, role, page) == EXPECTED[role]


def test_the_co_supervisor_has_nothing_to_report(team: SimpleNamespace) -> None:
    page = _page(team, "co_supervisor")
    tile = re.search(r'data-tile="alerts">.*?</li>', page, re.S)
    assert tile is not None and "Rien à signaler" in tile.group(0)
    assert "Aucune de vos actions n'est en retard." in page


# --------------------------------------------------------------------------- explicit lines


def _tile(page: str) -> str:
    tile = re.search(r'data-tile="alerts">.*?</li>\s*</ul>\s*</li>', page, re.S)
    assert tile is not None
    return tile.group(0)


def _plain(fragment: str) -> str:
    return re.sub(r"[ \t\r\n]+", " ", re.sub(r"<[^>]+>", " ", fragment)).strip()


def test_my_overdue_actions_are_named_with_a_link(team: SimpleNamespace) -> None:
    from apps.meetings.models import ActionItem

    tile = _tile(_page(team, "student"))
    assert "1 action de réunion en retard" in _plain(tile)
    action = ActionItem.objects.get(assignee=team.student)
    meeting = reverse(
        "meetings:detail", kwargs={"thesis_id": team.thesis.pk, "meeting_id": action.meeting_id}
    )
    line = re.search(rf'<a href="{meeting}">(.*?)</a>(.*?)</li>', tile, re.S)
    assert line is not None
    assert "Action fictive" in _plain(line.group(1))
    assert "15\u00a0janv." in _plain(line.group(2))


def test_decisions_and_receipts_link_to_their_element(team: SimpleNamespace) -> None:
    from apps.conferences.models import StApproval
    from apps.travel.models import Mission, MissionOrder

    kw = {"thesis_id": team.thesis.pk}
    tutor = _tile(_page(team, "tutor"))
    order = MissionOrder.objects.get(thesis=team.thesis, decision="PENDING")
    approval = StApproval.objects.get(thesis=team.thesis)
    assert "1 accord de mission à décider" in _plain(tutor)
    assert (
        f'href="{reverse("travel:mission", kwargs={**kw, "mission_id": order.mission_id})}"'
        in tutor
    )
    assert "1 accord ST à décider" in _plain(tutor)
    submission = reverse(
        "conferences:submission", kwargs={**kw, "submission_id": approval.submission_id}
    )
    assert f'href="{submission}"' in tutor
    student = _tile(_page(team, "student"))
    done = Mission.objects.get(thesis=team.thesis, status="DONE")
    assert "1 mission avec des justificatifs manquants" in _plain(student)
    assert f'href="{reverse("travel:mission", kwargs={**kw, "mission_id": done.pk})}"' in student


def test_the_tile_opens_sealed_texts_no_more(team: SimpleNamespace) -> None:
    from apps.vault.models import AuditEvent

    last = AuditEvent.objects.order_by("-seq").values_list("seq", flat=True).first()
    _page(team, "student")
    assert AuditEvent.objects.filter(seq__gt=last, action="SEALED_TEXT_READ").count() <= 1
